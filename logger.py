"""
logger.py

Sistema de logs coloridos para o terminal, usando apenas a biblioteca
padrão (sem dependências extras). Fornece funções semânticas para os
principais eventos do bot.

CATEGORIAS (v4.x) — cada evento relevante é logado com um prefixo entre
colchetes, permitindo filtrar por categoria (`grep '\\[crise\\]'
dados/logs/bot.log`, por exemplo) sem precisar de arquivos de log
separados por categoria (deliberadamente fora de escopo por ora — ver
README): [ia], [discord], [crise], [ticket], [helper], [painel],
[administracao]. Uma futura fase pode trocar isso por arquivos físicos
separados sem alterar a interface pública deste módulo (todas as
chamadas continuam `log.<funcao>(...)`).
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from datetime import datetime


class _Cores:
    RESET = "\033[0m"
    CINZA = "\033[90m"
    VERDE = "\033[92m"
    AMARELO = "\033[93m"
    VERMELHO = "\033[91m"
    AZUL = "\033[94m"
    CIANO = "\033[96m"
    NEGRITO = "\033[1m"


class _FormatterColorido(logging.Formatter):
    """Formatter que adiciona cor de acordo com o nível do log."""

    _CORES_POR_NIVEL = {
        logging.DEBUG: _Cores.CINZA,
        logging.INFO: _Cores.VERDE,
        logging.WARNING: _Cores.AMARELO,
        logging.ERROR: _Cores.VERMELHO,
        logging.CRITICAL: _Cores.VERMELHO + _Cores.NEGRITO,
    }

    def format(self, record: logging.LogRecord) -> str:
        cor = self._CORES_POR_NIVEL.get(record.levelno, _Cores.RESET)
        hora = datetime.now().strftime("%H:%M:%S")
        prefixo = f"{_Cores.CINZA}[{hora}]{_Cores.RESET} {cor}{record.levelname:<8}{_Cores.RESET}"
        return f"{prefixo} | {record.getMessage()}"


class _FormatterArquivo(logging.Formatter):
    """Formatter simples (sem códigos ANSI) usado no arquivo de log."""

    def format(self, record: logging.LogRecord) -> str:
        hora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return f"[{hora}] {record.levelname:<8} | {record.getMessage()}"


def _criar_logger() -> logging.Logger:
    logger = logging.getLogger("desabafos-bot")
    logger.setLevel(logging.DEBUG)

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_FormatterColorido())
        logger.addHandler(handler)

    return logger


log = _criar_logger()


def configurar_arquivo(caminho: str, dias_retencao: int) -> None:
    """
    Ativa a gravação dos logs em arquivo, com rotação diária. A limpeza
    de logs antigos é feita automaticamente pelo próprio
    TimedRotatingFileHandler (backupCount = dias_retencao), sem precisar
    de um job de limpeza manual para este item.
    """
    pasta = os.path.dirname(caminho)
    if pasta:
        os.makedirs(pasta, exist_ok=True)

    handler = logging.handlers.TimedRotatingFileHandler(
        caminho, when="midnight", backupCount=dias_retencao, encoding="utf-8"
    )
    handler.setFormatter(_FormatterArquivo())
    log.addHandler(handler)
    info(f"Logs em arquivo ativados em '{caminho}' (retenção de {dias_retencao} dias).")


def bot_conectado(nome_usuario: str, servidores: int) -> None:
    log.info(f"{_Cores.NEGRITO}Bot conectado{_Cores.RESET} como {nome_usuario} em {servidores} servidor(es).")


def usuario_atendido(usuario: str, tempo_resposta_ms: float) -> None:
    log.info(f"Usuário atendido: {_Cores.CIANO}{usuario}{_Cores.RESET} (resposta em {tempo_resposta_ms:.0f}ms)")


def mensagem_ignorada(motivo: str, usuario: str | None = None) -> None:
    alvo = f" ({usuario})" if usuario else ""
    log.debug(f"Mensagem ignorada{alvo}: {motivo}")


def erro(mensagem: str, exc: Exception | None = None) -> None:
    if exc is not None:
        log.error(f"{mensagem} -> {type(exc).__name__}: {exc}")
    else:
        log.error(mensagem)


def reconexao(tentativa: int) -> None:
    log.warning(f"Reconectando ao Discord... tentativa {tentativa}")


def aviso(mensagem: str) -> None:
    log.warning(mensagem)


def info(mensagem: str) -> None:
    log.info(mensagem)


def requisicao_ia(
    provedor: str,
    duracao_total_ms: float,
    duracao_ia_ms: float,
    tamanho_contexto: int,
    motivo_fallback: str = "",
) -> None:
    """Log detalhado de uma requisição de IA concluída com sucesso."""
    detalhe_fallback = f" | fallback: {motivo_fallback}" if motivo_fallback else ""
    log.debug(
        f"Requisição IA -> provedor={provedor} | total={duracao_total_ms:.0f}ms | "
        f"ia={duracao_ia_ms:.0f}ms | contexto~{tamanho_contexto} msgs{detalhe_fallback}"
    )


def watchdog(mensagem: str) -> None:
    log.warning(f"[watchdog] {mensagem}")


def backup(mensagem: str) -> None:
    log.info(f"[backup] {mensagem}")


def limpeza(mensagem: str) -> None:
    log.info(f"[limpeza] {mensagem}")


def ticket_criado(session_id: int, usuario: str, canal: str) -> None:
    log.info(f"[ticket] Sessão #{session_id} criada para {usuario} em '{canal}'.")


def ticket_fechado(session_id: int, duracao_legivel: str = "", quantidade_mensagens: int | None = None) -> None:
    detalhes = ""
    if duracao_legivel:
        detalhes += f" | duração: {duracao_legivel}"
    if quantidade_mensagens is not None:
        detalhes += f" | mensagens: {quantidade_mensagens}"
    log.info(f"[ticket] Sessão #{session_id} fechada{detalhes}.")


def sessao_reaberta(session_id: int) -> None:
    log.info(f"[ticket] Sessão #{session_id} reaberta (fechamento cancelado).")


def resumo_criado(session_id: int, modelo: str) -> None:
    log.info(f"[ticket] Resumo da sessão #{session_id} criado (modelo: {modelo}).")


def crise_detectada(session_id: int) -> None:
    log.warning(f"[crise] Indícios de crise detectados na sessão #{session_id}.")


def crise_escalada(session_id: int) -> None:
    log.warning(f"[crise] Escalada automática enviada para a sessão #{session_id} (Helper não assumiu a tempo).")


def helper_entrou(session_id: int, helper_id: int) -> None:
    log.warning(f"[helper] Helper {helper_id} assumiu a sessão #{session_id} — IA pausada.")


def painel_enviado(tipo: str, canal: str) -> None:
    log.info(f"[painel] Painel '{tipo}' publicado em '#{canal}' por comando administrativo.")


def configuracao_alterada(chave: str, quem: str) -> None:
    log.info(f"[administracao] Configuração '{chave}' alterada por {quem} via Painel MAIN.")
