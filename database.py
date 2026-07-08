"""
database.py

Camada de persistência em SQLite. Isolada do resto do bot: nenhum outro
módulo deve montar SQL diretamente, tudo passa pela classe Database.

Responsabilidades:
    - Migração automática (cria apenas tabelas/colunas ausentes, nunca
      recria o banco inteiro);
    - Blacklist de usuários;
    - Estatísticas de uso (persistidas);
    - Configurações administrativas persistentes;
    - Backup automático com rotação;
    - Cache em memória para reduzir consultas repetidas.

Todas as operações de I/O bloqueante rodam em threads separadas via
`asyncio.to_thread`, para nunca travar o event loop.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
from datetime import datetime, timezone

import logger as log

VERSAO_SCHEMA_ATUAL = 1

# Definição das tabelas geridas pela migração automática.
_TABELAS: dict[str, str] = {
    "schema_meta": """
        CREATE TABLE IF NOT EXISTS schema_meta (
            chave TEXT PRIMARY KEY,
            valor TEXT NOT NULL
        )
    """,
    "blacklist": """
        CREATE TABLE IF NOT EXISTS blacklist (
            usuario_id INTEGER PRIMARY KEY,
            motivo TEXT,
            criado_em TEXT NOT NULL
        )
    """,
    "configuracoes": """
        CREATE TABLE IF NOT EXISTS configuracoes (
            chave TEXT PRIMARY KEY,
            valor TEXT NOT NULL,
            atualizado_em TEXT NOT NULL
        )
    """,
    "estatisticas": """
        CREATE TABLE IF NOT EXISTS estatisticas (
            chave TEXT PRIMARY KEY,
            valor INTEGER NOT NULL DEFAULT 0
        )
    """,
    "usuarios_vistos": """
        CREATE TABLE IF NOT EXISTS usuarios_vistos (
            usuario_id INTEGER PRIMARY KEY,
            primeira_vez TEXT NOT NULL,
            ultima_vez TEXT NOT NULL
        )
    """,
}


class Database:
    """Camada de acesso ao SQLite, com migração automática e cache interno."""

    def __init__(self, caminho_db: str, pasta_backup: str) -> None:
        self._caminho_db = caminho_db
        self._pasta_backup = pasta_backup
        self._conexao: sqlite3.Connection | None = None

        # Cache em memória (reduz consultas repetidas ao banco).
        self._cache_blacklist: set[int] = set()
        self._cache_config: dict[str, str] = {}
        self._cache_usuarios_ativos: set[int] = set()

    # ------------------------------------------------------------------
    # Ciclo de vida
    # ------------------------------------------------------------------

    async def iniciar(self) -> None:
        """Conecta ao banco, roda migrações e carrega o cache inicial."""
        await asyncio.to_thread(self._conectar_e_migrar_sync)
        await self._carregar_cache()
        log.info(f"Banco de dados pronto em '{self._caminho_db}' (schema v{VERSAO_SCHEMA_ATUAL}).")

    def _conectar_e_migrar_sync(self) -> None:
        pasta = os.path.dirname(self._caminho_db)
        if pasta:
            os.makedirs(pasta, exist_ok=True)

        self._conexao = sqlite3.connect(self._caminho_db, check_same_thread=False)
        self._conexao.row_factory = sqlite3.Row
        self._conexao.execute("PRAGMA journal_mode=WAL")
        self._migrar_sync()

    def _migrar_sync(self) -> None:
        """
        Cria apenas as tabelas ausentes (via CREATE TABLE IF NOT EXISTS).
        Nunca recria o banco inteiro nem apaga dados existentes.
        """
        assert self._conexao is not None
        cursor = self._conexao.cursor()

        for sql_criacao in _TABELAS.values():
            cursor.execute(sql_criacao)

        cursor.execute(
            "INSERT OR IGNORE INTO schema_meta (chave, valor) VALUES ('versao_schema', ?)",
            (str(VERSAO_SCHEMA_ATUAL),),
        )
        self._conexao.commit()
        log.info("Migração de banco de dados verificada (nenhuma estrutura recriada).")

    async def fechar(self) -> None:
        """Fecha a conexão com o banco."""
        if self._conexao is not None:
            await asyncio.to_thread(self._conexao.close)

    async def _carregar_cache(self) -> None:
        """Carrega blacklist e configurações persistentes para a memória."""

        def _sync() -> tuple[set[int], dict[str, str]]:
            assert self._conexao is not None
            cursor = self._conexao.cursor()

            cursor.execute("SELECT usuario_id FROM blacklist")
            blacklist = {row["usuario_id"] for row in cursor.fetchall()}

            cursor.execute("SELECT chave, valor FROM configuracoes")
            configuracoes = {row["chave"]: row["valor"] for row in cursor.fetchall()}

            return blacklist, configuracoes

        blacklist, configuracoes = await asyncio.to_thread(_sync)
        self._cache_blacklist = blacklist
        self._cache_config = configuracoes

    # ------------------------------------------------------------------
    # Blacklist
    # ------------------------------------------------------------------

    def esta_na_blacklist(self, usuario_id: int) -> bool:
        """Consulta a blacklist via cache em memória (sem tocar o disco)."""
        return usuario_id in self._cache_blacklist

    async def adicionar_blacklist(self, usuario_id: int, motivo: str = "") -> None:
        """Adiciona um usuário à blacklist e persiste no banco."""
        agora = _agora_iso()

        def _sync() -> None:
            assert self._conexao is not None
            self._conexao.execute(
                "INSERT OR REPLACE INTO blacklist (usuario_id, motivo, criado_em) VALUES (?, ?, ?)",
                (usuario_id, motivo, agora),
            )
            self._conexao.commit()

        await asyncio.to_thread(_sync)
        self._cache_blacklist.add(usuario_id)

    async def remover_blacklist(self, usuario_id: int) -> None:
        """Remove um usuário da blacklist."""

        def _sync() -> None:
            assert self._conexao is not None
            self._conexao.execute("DELETE FROM blacklist WHERE usuario_id = ?", (usuario_id,))
            self._conexao.commit()

        await asyncio.to_thread(_sync)
        self._cache_blacklist.discard(usuario_id)

    # ------------------------------------------------------------------
    # Configurações persistentes
    # ------------------------------------------------------------------

    def obter_configuracao(self, chave: str, padrao: str | None = None) -> str | None:
        """Lê uma configuração do cache em memória."""
        return self._cache_config.get(chave, padrao)

    async def definir_configuracao(self, chave: str, valor: str) -> None:
        """
        Define/atualiza uma configuração administrativa. Fica salva no
        SQLite e permanece após reiniciar o bot.
        """
        agora = _agora_iso()

        def _sync() -> None:
            assert self._conexao is not None
            self._conexao.execute(
                """
                INSERT INTO configuracoes (chave, valor, atualizado_em)
                VALUES (?, ?, ?)
                ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor, atualizado_em = excluded.atualizado_em
                """,
                (chave, valor, agora),
            )
            self._conexao.commit()

        await asyncio.to_thread(_sync)
        self._cache_config[chave] = valor

    # ------------------------------------------------------------------
    # Estatísticas
    # ------------------------------------------------------------------

    async def incrementar_estatistica(self, chave: str, incremento: int = 1) -> None:
        """Incrementa (ou cria) um contador de estatística persistente."""

        def _sync() -> None:
            assert self._conexao is not None
            self._conexao.execute(
                """
                INSERT INTO estatisticas (chave, valor) VALUES (?, ?)
                ON CONFLICT(chave) DO UPDATE SET valor = valor + excluded.valor
                """,
                (chave, incremento),
            )
            self._conexao.commit()

        await asyncio.to_thread(_sync)

    async def registrar_usuario_visto(self, usuario_id: int) -> None:
        """Registra (ou atualiza) que um usuário único interagiu com o bot."""
        agora = _agora_iso()
        novo = usuario_id not in self._cache_usuarios_ativos

        def _sync() -> None:
            assert self._conexao is not None
            self._conexao.execute(
                """
                INSERT INTO usuarios_vistos (usuario_id, primeira_vez, ultima_vez)
                VALUES (?, ?, ?)
                ON CONFLICT(usuario_id) DO UPDATE SET ultima_vez = excluded.ultima_vez
                """,
                (usuario_id, agora, agora),
            )
            self._conexao.commit()

        await asyncio.to_thread(_sync)
        self._cache_usuarios_ativos.add(usuario_id)
        if novo:
            await self.incrementar_estatistica("usuarios_unicos", 1)

    async def obter_estatisticas_completas(self) -> dict[str, int]:
        """Retorna todas as estatísticas persistidas, prontas para exibição (ex: /health)."""

        def _sync() -> dict[str, int]:
            assert self._conexao is not None
            cursor = self._conexao.execute("SELECT chave, valor FROM estatisticas")
            return {row["chave"]: row["valor"] for row in cursor.fetchall()}

        return await asyncio.to_thread(_sync)

    # ------------------------------------------------------------------
    # Backup automático
    # ------------------------------------------------------------------

    async def criar_backup(self, manter_max: int) -> str | None:
        """
        Cria uma cópia local do banco de dados com timestamp e remove
        backups excedentes, mantendo apenas os `manter_max` mais recentes.
        """

        def _sync() -> str | None:
            if not os.path.exists(self._caminho_db):
                return None

            os.makedirs(self._pasta_backup, exist_ok=True)
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            nome_arquivo = f"desabafos-{timestamp}.db"
            destino = os.path.join(self._pasta_backup, nome_arquivo)

            shutil.copyfile(self._caminho_db, destino)

            backups = sorted(
                f for f in os.listdir(self._pasta_backup) if f.startswith("desabafos-") and f.endswith(".db")
            )
            excedentes = len(backups) - manter_max
            for antigo in backups[: max(0, excedentes)]:
                os.remove(os.path.join(self._pasta_backup, antigo))

            return destino

        destino = await asyncio.to_thread(_sync)
        if destino:
            log.info(f"Backup do banco de dados criado: {destino}")
        return destino

    # ------------------------------------------------------------------
    # Verificação de saúde (usada pelo /health)
    # ------------------------------------------------------------------

    async def esta_saudavel(self) -> bool:
        """Executa uma consulta simples para confirmar que o banco responde."""

        def _sync() -> bool:
            assert self._conexao is not None
            self._conexao.execute("SELECT 1")
            return True

        try:
            return await asyncio.to_thread(_sync)
        except Exception as exc:  # noqa: BLE001
            log.erro("Falha na verificação de saúde do banco de dados", exc)
            return False


def _agora_iso() -> str:
    """Retorna o timestamp atual em ISO 8601 (UTC)."""
    return datetime.now(timezone.utc).isoformat()
