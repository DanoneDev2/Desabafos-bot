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

VERSAO_SCHEMA_ATUAL = 3

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
    # --- Sistema de Tickets / Sessões privadas (v3.0) ---------------------
    "sessions": """
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            opened_at TEXT NOT NULL,
            closed_at TEXT,
            status TEXT NOT NULL DEFAULT 'aberta',
            summary TEXT,
            main_topics TEXT,
            concerns TEXT,
            goals TEXT,
            emotion TEXT,
            message_count INTEGER NOT NULL DEFAULT 0,
            last_activity TEXT NOT NULL,
            aviso_inatividade_enviado INTEGER NOT NULL DEFAULT 0,
            crise_detectada INTEGER NOT NULL DEFAULT 0
        )
    """,
    "messages": """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            emotion TEXT,
            created_at TEXT NOT NULL
        )
    """,
    # --- Helpers humanos e avaliação (v4.0) --------------------------------
    "avaliacoes": """
        CREATE TABLE IF NOT EXISTS avaliacoes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            estrelas INTEGER NOT NULL,
            comentario TEXT,
            criado_em TEXT NOT NULL
        )
    """,
}

# Colunas esperadas por tabela — usado pela migração para ADICIONAR
# colunas que ainda não existem em bancos criados por versões antigas,
# sem nunca recriar ou apagar a tabela.
_COLUNAS_ESPERADAS: dict[str, dict[str, str]] = {
    "sessions": {
        "summary": "TEXT",
        "main_topics": "TEXT",
        "concerns": "TEXT",
        "goals": "TEXT",
        "emotion": "TEXT",
        "aviso_inatividade_enviado": "INTEGER NOT NULL DEFAULT 0",
        "crise_detectada": "INTEGER NOT NULL DEFAULT 0",
        "modo": "TEXT NOT NULL DEFAULT 'ia'",
        "helper_id": "INTEGER",
    },
    "messages": {
        "emotion": "TEXT",
    },
}

_INDICES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_sessions_user_status ON sessions(user_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_channel ON sessions(channel_id)",
    "CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id)",
)


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
        Cria apenas as tabelas ausentes (via CREATE TABLE IF NOT EXISTS) e
        adiciona apenas as colunas ausentes em tabelas já existentes (via
        PRAGMA table_info + ALTER TABLE ADD COLUMN). Nunca recria o banco
        inteiro nem apaga dados existentes.
        """
        assert self._conexao is not None
        cursor = self._conexao.cursor()

        for sql_criacao in _TABELAS.values():
            cursor.execute(sql_criacao)

        for tabela, colunas in _COLUNAS_ESPERADAS.items():
            colunas_existentes = {
                row["name"] for row in cursor.execute(f"PRAGMA table_info({tabela})").fetchall()
            }
            for coluna, definicao_sql in colunas.items():
                if coluna not in colunas_existentes:
                    cursor.execute(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {definicao_sql}")
                    log.info(f"Migração: coluna '{coluna}' adicionada à tabela '{tabela}'.")

        for sql_indice in _INDICES:
            cursor.execute(sql_indice)

        cursor.execute(
            "INSERT OR REPLACE INTO schema_meta (chave, valor) VALUES ('versao_schema', ?)",
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
    # Sistema de Tickets / Sessões privadas (v3.0)
    # ------------------------------------------------------------------

    async def criar_sessao(self, user_id: int, channel_id: int) -> int:
        """Cria uma nova sessão (ticket) 'aberta' e retorna seu id."""
        agora = _agora_iso()

        def _sync() -> int:
            assert self._conexao is not None
            cursor = self._conexao.execute(
                """
                INSERT INTO sessions (user_id, channel_id, opened_at, status, last_activity)
                VALUES (?, ?, ?, 'aberta', ?)
                """,
                (user_id, channel_id, agora, agora),
            )
            self._conexao.commit()
            return int(cursor.lastrowid)

        return await asyncio.to_thread(_sync)

    async def obter_sessao_ativa_por_usuario(self, user_id: int) -> dict | None:
        """Retorna a sessão 'aberta' de um usuário, se existir."""

        def _sync() -> dict | None:
            assert self._conexao is not None
            row = self._conexao.execute(
                "SELECT * FROM sessions WHERE user_id = ? AND status = 'aberta' ORDER BY id DESC LIMIT 1",
                (user_id,),
            ).fetchone()
            return dict(row) if row else None

        return await asyncio.to_thread(_sync)

    async def obter_ultima_sessao_fechada(self, user_id: int) -> dict | None:
        """Retorna a sessão 'fechada' mais recente de um usuário (usada para dar continuidade)."""

        def _sync() -> dict | None:
            assert self._conexao is not None
            row = self._conexao.execute(
                "SELECT * FROM sessions WHERE user_id = ? AND status = 'fechada' ORDER BY closed_at DESC LIMIT 1",
                (user_id,),
            ).fetchone()
            return dict(row) if row else None

        return await asyncio.to_thread(_sync)

    async def obter_sessao_por_canal(self, channel_id: int) -> dict | None:
        """Retorna a sessão associada a um canal/thread, se existir (aberta ou não)."""

        def _sync() -> dict | None:
            assert self._conexao is not None
            row = self._conexao.execute(
                "SELECT * FROM sessions WHERE channel_id = ? ORDER BY id DESC LIMIT 1",
                (channel_id,),
            ).fetchone()
            return dict(row) if row else None

        return await asyncio.to_thread(_sync)

    async def obter_sessao(self, session_id: int) -> dict | None:
        """Retorna uma sessão pelo seu id."""

        def _sync() -> dict | None:
            assert self._conexao is not None
            row = self._conexao.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
            return dict(row) if row else None

        return await asyncio.to_thread(_sync)

    async def contar_sessoes_abertas(self) -> int:
        """Conta quantas sessões estão com status 'aberta' (usado para SESSION_LIMIT e /health)."""

        def _sync() -> int:
            assert self._conexao is not None
            row = self._conexao.execute("SELECT COUNT(*) AS total FROM sessions WHERE status = 'aberta'").fetchone()
            return int(row["total"])

        return await asyncio.to_thread(_sync)

    async def contar_sessoes_fechadas(self) -> int:
        """Conta quantas sessões já foram encerradas (usado no /health)."""

        def _sync() -> int:
            assert self._conexao is not None
            row = self._conexao.execute("SELECT COUNT(*) AS total FROM sessions WHERE status = 'fechada'").fetchone()
            return int(row["total"])

        return await asyncio.to_thread(_sync)

    async def registrar_mensagem_sessao(
        self, session_id: int, role: str, content: str, emotion: str | None = None
    ) -> None:
        """Salva uma mensagem da sessão, incrementa o contador e atualiza a última atividade."""
        agora = _agora_iso()

        def _sync() -> None:
            assert self._conexao is not None
            self._conexao.execute(
                "INSERT INTO messages (session_id, role, content, emotion, created_at) VALUES (?, ?, ?, ?, ?)",
                (session_id, role, content, emotion, agora),
            )
            self._conexao.execute(
                """
                UPDATE sessions
                SET message_count = message_count + 1, last_activity = ?, aviso_inatividade_enviado = 0
                WHERE id = ?
                """,
                (agora, session_id),
            )
            if emotion:
                self._conexao.execute("UPDATE sessions SET emotion = ? WHERE id = ?", (emotion, session_id))
            self._conexao.commit()

        await asyncio.to_thread(_sync)

    async def obter_mensagens_sessao(self, session_id: int) -> list[dict]:
        """Retorna todas as mensagens de uma sessão, em ordem cronológica."""

        def _sync() -> list[dict]:
            assert self._conexao is not None
            rows = self._conexao.execute(
                "SELECT role, content, emotion, created_at FROM messages WHERE session_id = ? ORDER BY id ASC",
                (session_id,),
            ).fetchall()
            return [dict(row) for row in rows]

        return await asyncio.to_thread(_sync)

    async def fechar_sessao(
        self,
        session_id: int,
        summary: str = "",
        main_topics: str = "",
        concerns: str = "",
        goals: str = "",
        emotion: str = "",
    ) -> None:
        """Marca uma sessão como 'fechada' e salva o resumo gerado ao encerrar."""
        agora = _agora_iso()

        def _sync() -> None:
            assert self._conexao is not None
            self._conexao.execute(
                """
                UPDATE sessions
                SET status = 'fechada', closed_at = ?, summary = ?, main_topics = ?, concerns = ?, goals = ?,
                    emotion = COALESCE(NULLIF(?, ''), emotion)
                WHERE id = ?
                """,
                (agora, summary, main_topics, concerns, goals, emotion, session_id),
            )
            self._conexao.commit()

        await asyncio.to_thread(_sync)

    async def reabrir_sessao(self, session_id: int) -> None:
        """Cancela um fechamento pendente: mantém a sessão 'aberta' e reseta o aviso de inatividade."""
        agora = _agora_iso()

        def _sync() -> None:
            assert self._conexao is not None
            self._conexao.execute(
                "UPDATE sessions SET status = 'aberta', last_activity = ?, aviso_inatividade_enviado = 0 WHERE id = ?",
                (agora, session_id),
            )
            self._conexao.commit()

        await asyncio.to_thread(_sync)

    async def marcar_aviso_inatividade_enviado(self, session_id: int) -> None:
        """Marca que o aviso de 'posso encerrar?' já foi enviado, evitando repetição."""

        def _sync() -> None:
            assert self._conexao is not None
            self._conexao.execute(
                "UPDATE sessions SET aviso_inatividade_enviado = 1 WHERE id = ?", (session_id,)
            )
            self._conexao.commit()

        await asyncio.to_thread(_sync)

    async def marcar_crise(self, session_id: int) -> None:
        """Marca que uma sessão teve um episódio de crise detectado."""

        def _sync() -> None:
            assert self._conexao is not None
            self._conexao.execute("UPDATE sessions SET crise_detectada = 1 WHERE id = ?", (session_id,))
            self._conexao.commit()

        await asyncio.to_thread(_sync)
        await self.incrementar_estatistica("crises_detectadas")

    # ------------------------------------------------------------------
    # Helpers humanos e avaliação (v4.0)
    # ------------------------------------------------------------------

    async def definir_modo_sessao(self, session_id: int, modo: str, helper_id: int | None = None) -> None:
        """
        Define o modo de atendimento da sessão: 'ia' (padrão), 'observador'
        (a IA para de responder enquanto um Helper humano assume) ou
        'cooperacao' (a IA sugere respostas apenas para o Helper ver).
        """
        if modo not in ("ia", "observador", "cooperacao"):
            raise ValueError(f"Modo de sessão inválido: {modo}")

        def _sync() -> None:
            assert self._conexao is not None
            self._conexao.execute(
                "UPDATE sessions SET modo = ?, helper_id = ? WHERE id = ?",
                (modo, helper_id, session_id),
            )
            self._conexao.commit()

        await asyncio.to_thread(_sync)

    async def salvar_avaliacao(
        self, session_id: int, user_id: int, estrelas: int, comentario: str | None = None
    ) -> None:
        """Salva a avaliação (1 a 5 estrelas + comentário opcional) dada após o encerramento de uma sessão."""
        agora = _agora_iso()

        def _sync() -> None:
            assert self._conexao is not None
            self._conexao.execute(
                "INSERT INTO avaliacoes (session_id, user_id, estrelas, comentario, criado_em) VALUES (?, ?, ?, ?, ?)",
                (session_id, user_id, estrelas, comentario, agora),
            )
            self._conexao.commit()

        await asyncio.to_thread(_sync)
        await self.incrementar_estatistica("avaliacoes_recebidas")
        if estrelas <= 2:
            await self.incrementar_estatistica("avaliacoes_negativas")

    async def obter_media_avaliacoes(self) -> tuple[float, int]:
        """Retorna a (média de estrelas, quantidade de avaliações) — usado no /health."""

        def _sync() -> tuple[float, int]:
            assert self._conexao is not None
            row = self._conexao.execute(
                "SELECT AVG(estrelas) AS media, COUNT(*) AS total FROM avaliacoes"
            ).fetchone()
            media = float(row["media"]) if row["media"] is not None else 0.0
            return media, int(row["total"])

        return await asyncio.to_thread(_sync)

    async def listar_sessoes_abertas_para_verificacao(self) -> list[dict]:
        """Retorna todas as sessões abertas, para a rotina de fechamento automático avaliar inatividade."""

        def _sync() -> list[dict]:
            assert self._conexao is not None
            rows = self._conexao.execute("SELECT * FROM sessions WHERE status = 'aberta'").fetchall()
            return [dict(row) for row in rows]

        return await asyncio.to_thread(_sync)

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

    async def ultimo_backup_info(self) -> str | None:
        """Retorna o nome do backup mais recente encontrado, ou None se não houver nenhum."""

        def _sync() -> str | None:
            if not os.path.isdir(self._pasta_backup):
                return None
            backups = sorted(
                f for f in os.listdir(self._pasta_backup) if f.startswith("desabafos-") and f.endswith(".db")
            )
            return backups[-1] if backups else None

        return await asyncio.to_thread(_sync)


def _agora_iso() -> str:
    """Retorna o timestamp atual em ISO 8601 (UTC)."""
    return datetime.now(timezone.utc).isoformat()
