"""
main.py

Ponto de entrada do bot. Responsável apenas por validar a configuração
e iniciar o cliente Discord — nenhuma lógica de negócio deve viver aqui.
"""

from __future__ import annotations

import sys

import discord

import logger as log
from config import config
from database import Database
from event_bus import bus
from events import BotDeDesabafos


def _registrar_ouvintes_padrao() -> None:
    """
    Ouvintes mínimos do Event Bus, apenas para log — servem de exemplo
    concreto de como um módulo futuro (música, timeline, analytics,
    base de conhecimento...) pode se inscrever em eventos do bot sem
    precisar alterar nenhum módulo existente.
    """
    bus.on("sessao_criada", lambda **d: log.info(f"[evento] sessao_criada: {d}"))
    bus.on("sessao_encerrada", lambda **d: log.info(f"[evento] sessao_encerrada: sessão #{d.get('session_id')}"))
    bus.on("helper_entrou", lambda **d: log.info(f"[evento] helper_entrou: {d}"))
    bus.on("avaliacao_recebida", lambda **d: log.info(f"[evento] avaliacao_recebida: {d}"))


def main() -> None:
    """Valida a configuração e inicia o bot."""
    problemas = config.validar()
    if problemas:
        log.erro("Configuração inválida, corrija o arquivo .env:")
        for problema in problemas:
            log.erro(f"  - {problema}")
        sys.exit(1)

    log.configurar_arquivo("dados/logs/bot.log", config.logs_retencao_dias)
    log.info("Iniciando Bot de Desabafos...")

    _registrar_ouvintes_padrao()

    db = Database(caminho_db=config.db_path, pasta_backup=config.db_backup_dir)
    bot = BotDeDesabafos(config, db)

    try:
        bot.run(config.token_discord, log_handler=None)
    except discord.LoginFailure as exc:
        log.erro("Falha de login: verifique se TOKEN_DISCORD está correto", exc)
        sys.exit(1)
    except KeyboardInterrupt:
        log.info("Bot encerrado manualmente (Ctrl+C).")
    except Exception as exc:  # noqa: BLE001
        log.erro("Erro fatal ao iniciar o bot", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
