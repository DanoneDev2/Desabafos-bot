"""
config.py

Centraliza toda a configuração do bot, carregada a partir de variáveis
de ambiente (.env). Nenhuma informação sensível deve ficar hardcoded
no código-fonte.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _get_int(env_name: str, default: int) -> int:
    """Lê uma variável de ambiente como inteiro, com fallback seguro."""
    raw = os.getenv(env_name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _get_float(env_name: str, default: float) -> float:
    """Lê uma variável de ambiente como float, com fallback seguro."""
    raw = os.getenv(env_name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _get_bool(env_name: str, default: bool) -> bool:
    """Lê uma variável de ambiente como booleano ('true'/'1'/'sim' contam como verdadeiro)."""
    raw = os.getenv(env_name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "sim", "yes", "on")


@dataclass(frozen=True)
class Config:
    """Configuração imutável do bot, montada a partir do ambiente."""

    # Discord
    token_discord: str = field(default_factory=lambda: os.getenv("TOKEN_DISCORD", ""))
    canal_desabafos: int = field(default_factory=lambda: _get_int("CANAL_DESABAFOS", 0))

    # Sistema de Tickets / Sessões privadas (v3.0)
    # ATENÇÃO: a partir da v3.0, CANAL_DESABAFOS deixa de ser o canal onde
    # todos conversam e passa a ser o canal onde o PAINEL ("Iniciar
    # Conversa") é publicado. Isso preserva compatibilidade com quem já
    # tinha essa variável configurada.
    staff_role_id: int = field(default_factory=lambda: _get_int("STAFF_ROLE_ID", 0))
    # Cargo dos Helpers humanos (v4.0): voluntários que podem assumir uma
    # conversa (Modo Observador/Cooperação). Se vazio, usa STAFF_ROLE_ID.
    helper_role_id: int = field(default_factory=lambda: _get_int("HELPER_ROLE_ID", 0))
    category_tickets: int = field(default_factory=lambda: _get_int("CATEGORY_TICKETS", 0))
    auto_close_horas: float = field(default_factory=lambda: _get_float("AUTO_CLOSE_HOURS", 48.0))
    auto_close_tolerancia_horas: float = field(
        default_factory=lambda: _get_float("AUTO_CLOSE_TOLERANCIA_HORAS", 6.0)
    )
    ticket_check_intervalo_minutos: int = field(
        default_factory=lambda: _get_int("TICKET_CHECK_INTERVALO_MINUTOS", 30)
    )
    summary_model: str = field(default_factory=lambda: os.getenv("SUMMARY_MODEL", ""))
    session_limit: int = field(default_factory=lambda: _get_int("SESSION_LIMIT", 0))
    enable_crisis_mode: bool = field(default_factory=lambda: _get_bool("ENABLE_CRISIS_MODE", True))
    enable_streaming: bool = field(default_factory=lambda: _get_bool("ENABLE_STREAMING", False))
    enable_private_threads: bool = field(default_factory=lambda: _get_bool("ENABLE_PRIVATE_THREADS", False))

    # Cargos adicionais e canais dedicados (v4.x) — todos com override
    # possível pelo Painel MAIN (ver valor_efetivo* abaixo); os valores
    # aqui são apenas o padrão vindo do .env.
    supervisor_role_id: int = field(default_factory=lambda: _get_int("SUPERVISOR_ROLE_ID", 0))
    admin_role_id: int = field(default_factory=lambda: _get_int("ADMIN_ROLE_ID", 0))
    canal_transcripts: int = field(default_factory=lambda: _get_int("CANAL_TRANSCRIPTS", 0))
    canal_logs: int = field(default_factory=lambda: _get_int("CANAL_LOGS", 0))
    canal_alertas: int = field(default_factory=lambda: _get_int("CANAL_ALERTAS", 0))
    canal_admin: int = field(default_factory=lambda: _get_int("CANAL_ADMIN", 0))
    canal_crise_grave: int = field(default_factory=lambda: _get_int("CANAL_CRISE_GRAVE", 0))
    canal_chamada_helpers: int = field(default_factory=lambda: _get_int("CANAL_CHAMADA_HELPERS", 0))

    # Escalada de crise (v4.x)
    crise_tempo_maximo_espera_helper_minutos: int = field(
        default_factory=lambda: _get_int("CRISE_TEMPO_MAXIMO_ESPERA_HELPER_MINUTOS", 15)
    )
    crise_escalada_automatica: bool = field(default_factory=lambda: _get_bool("CRISE_ESCALADA_AUTOMATICA", True))

    # IA
    api_key_gemini: str = field(default_factory=lambda: os.getenv("API_KEY_GEMINI", os.getenv("API_KEY_IA", "")))
    api_key_groq: str = field(default_factory=lambda: os.getenv("API_KEY_GROQ", ""))
    model_name: str = field(default_factory=lambda: os.getenv("MODEL_NAME", "gemini-1.5-flash"))
    groq_model_name: str = field(default_factory=lambda: os.getenv("GROQ_MODEL_NAME", "llama-3.1-70b-versatile"))
    temperature: float = field(default_factory=lambda: _get_float("TEMPERATURE", 0.9))

    # Memória
    max_history: int = field(default_factory=lambda: _get_int("MAX_HISTORY", 10))
    contexto_maximo_envio: int = field(default_factory=lambda: _get_int("CONTEXTO_MAXIMO_ENVIO", 0))
    resumo_max_caracteres: int = field(default_factory=lambda: _get_int("RESUMO_MAX_CARACTERES", 800))
    memoria_inatividade_max_segundos: int = field(
        default_factory=lambda: _get_int("MEMORIA_INATIVIDADE_MAX_SEGUNDOS", 3600 * 6)
    )

    # Segurança / Anti-spam
    cooldown_segundos: int = field(default_factory=lambda: _get_int("COOLDOWN_SEGUNDOS", 5))
    tamanho_maximo_mensagem: int = field(default_factory=lambda: _get_int("TAMANHO_MAXIMO_MENSAGEM", 1500))

    # Fila assíncrona de requisições à IA
    fila_ia_tamanho_maximo: int = field(default_factory=lambda: _get_int("FILA_IA_TAMANHO_MAXIMO", 50))

    # Retry / Backoff exponencial
    retry_max_tentativas: int = field(default_factory=lambda: _get_int("RETRY_MAX_TENTATIVAS", 3))
    retry_backoff_base_segundos: float = field(
        default_factory=lambda: _get_float("RETRY_BACKOFF_BASE_SEGUNDOS", 1.0)
    )
    retry_backoff_teto_segundos: float = field(
        default_factory=lambda: _get_float("RETRY_BACKOFF_TETO_SEGUNDOS", 20.0)
    )

    # Circuit Breaker
    circuit_breaker_falhas_consecutivas: int = field(
        default_factory=lambda: _get_int("CIRCUIT_BREAKER_FALHAS_CONSECUTIVAS", 3)
    )
    circuit_breaker_timeout_segundos: int = field(
        default_factory=lambda: _get_int("CIRCUIT_BREAKER_TIMEOUT_SEGUNDOS", 60)
    )

    # SQLite
    db_path: str = field(default_factory=lambda: os.getenv("DB_PATH", "dados/desabafos.db"))
    db_backup_dir: str = field(default_factory=lambda: os.getenv("DB_BACKUP_DIR", "dados/backups"))
    db_backup_max: int = field(default_factory=lambda: _get_int("DB_BACKUP_MAX", 5))
    db_backup_intervalo_horas: int = field(default_factory=lambda: _get_int("DB_BACKUP_INTERVALO_HORAS", 24))

    # Limpeza automática
    limpeza_intervalo_horas: int = field(default_factory=lambda: _get_int("LIMPEZA_INTERVALO_HORAS", 6))
    logs_retencao_dias: int = field(default_factory=lambda: _get_int("LOGS_RETENCAO_DIAS", 7))

    # Watchdog
    watchdog_intervalo_segundos: int = field(default_factory=lambda: _get_int("WATCHDOG_INTERVALO_SEGUNDOS", 30))

    def validar(self) -> list[str]:
        """Retorna uma lista de problemas de configuração encontrados (variáveis críticas ausentes)."""
        problemas: list[str] = []

        if not self.token_discord:
            problemas.append("TOKEN_DISCORD não foi definido no .env")

        if not self.canal_desabafos:
            problemas.append("CANAL_DESABAFOS não foi definido (ou é inválido) no .env")

        if not self.api_key_gemini and not self.api_key_groq:
            problemas.append(
                "Nenhuma chave de IA definida. Configure API_KEY_GEMINI e/ou API_KEY_GROQ no .env"
            )

        if self.retry_max_tentativas < 1:
            problemas.append("RETRY_MAX_TENTATIVAS deve ser maior ou igual a 1")

        if self.circuit_breaker_falhas_consecutivas < 1:
            problemas.append("CIRCUIT_BREAKER_FALHAS_CONSECUTIVAS deve ser maior ou igual a 1")

        if not self.enable_private_threads and not self.category_tickets:
            problemas.append(
                "CATEGORY_TICKETS não foi definido (obrigatório para criar canais de ticket; "
                "defina ENABLE_PRIVATE_THREADS=true para usar threads privadas em vez de categoria)"
            )

        if self.auto_close_horas <= 0:
            problemas.append("AUTO_CLOSE_HOURS deve ser maior que 0")

        return problemas

    def contexto_efetivo(self) -> int:
        """
        Retorna o limite real de mensagens (pares pergunta/resposta) enviadas
        à IA. Se CONTEXTO_MAXIMO_ENVIO não for definido (0), usa max_history.
        """
        if self.contexto_maximo_envio and self.contexto_maximo_envio > 0:
            return min(self.contexto_maximo_envio, self.max_history)
        return self.max_history

    @property
    def canal_painel(self) -> int:
        """Canal onde o painel permanente ('Iniciar Conversa') é publicado."""
        return self.canal_desabafos

    @property
    def modelo_resumo(self) -> str:
        """Modelo Gemini usado para gerar resumos de sessão (SUMMARY_MODEL ou, por padrão, MODEL_NAME)."""
        return self.summary_model or self.model_name

    @property
    def cargos_de_apoio(self) -> tuple[int, ...]:
        """IDs dos cargos que podem assumir uma conversa como Helper (Staff + Helper, sem duplicar)."""
        return tuple({cargo for cargo in (self.staff_role_id, self.helper_role_id) if cargo})

    def cargos_de_apoio_efetivo(self, db=None) -> tuple[int, ...]:
        """
        IDs dos cargos de apoio (Staff + Helper(s)), considerando um
        possível override do painel administrativo (múltiplos cargos
        Helper, separados por vírgula, sob a chave `cargos_apoio_ids`).
        """
        return lista_efetiva_ids(db, "cargos_apoio_ids", self.cargos_de_apoio)

    def canal_efetivo(self, db, chave: str, padrao: int) -> int:
        """Resolve o ID de um canal configurável (transcripts, logs, alertas, crise, etc.)."""
        return valor_efetivo_int(db, chave, padrao)


config = Config()


# ----------------------------------------------------------------------
# Resolução de "configuração efetiva" (v4.x) — um valor salvo pelo painel
# administrativo (tabela `configuracoes` do SQLite) sempre tem prioridade
# sobre o `.env`; se nada tiver sido salvo, o `.env` continua valendo.
#
# Centralizado aqui para todo módulo (ai.py, ticket_manager.py, events.py)
# reaproveitar a MESMA lógica em vez de reimplementar o padrão
# "tenta converter, se falhar cai no padrão" em cada lugar (regra do
# projeto: evitar código duplicado). `db` é sempre um objeto com o
# método síncrono `obter_configuracao(chave) -> str | None` (a classe
# `Database`); passar `None` sempre retorna o padrão.
# ----------------------------------------------------------------------


def valor_efetivo(db, chave: str, padrao: str) -> str:
    """Resolve um valor de configuração como texto (painel administrativo > padrão)."""
    if db is None:
        return padrao
    bruto = db.obter_configuracao(chave)
    return bruto if bruto not in (None, "") else padrao


def valor_efetivo_int(db, chave: str, padrao: int) -> int:
    """Resolve um valor de configuração como inteiro, com fallback seguro para o padrão."""
    bruto = valor_efetivo(db, chave, str(padrao))
    try:
        return int(bruto)
    except (TypeError, ValueError):
        return padrao


def valor_efetivo_float(db, chave: str, padrao: float) -> float:
    """Resolve um valor de configuração como float, com fallback seguro para o padrão."""
    bruto = valor_efetivo(db, chave, str(padrao))
    try:
        return float(bruto)
    except (TypeError, ValueError):
        return padrao


def valor_efetivo_bool(db, chave: str, padrao: bool) -> bool:
    """Resolve um valor de configuração como booleano ('sim'/'true'/'1' contam como verdadeiro)."""
    bruto = valor_efetivo(db, chave, "true" if padrao else "false")
    return str(bruto).strip().lower() in ("true", "sim", "1", "yes")


def lista_efetiva_ids(db, chave: str, padrao: tuple[int, ...]) -> tuple[int, ...]:
    """Resolve uma lista de IDs (cargos, canais) separada por vírgula, com fallback para o padrão."""
    bruto = valor_efetivo(db, chave, "")
    if not bruto:
        return padrao
    ids: list[int] = []
    for pedaco in bruto.split(","):
        pedaco = pedaco.strip()
        if pedaco.isdigit():
            ids.append(int(pedaco))
    return tuple(ids) if ids else padrao
