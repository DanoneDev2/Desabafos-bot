"""
scheduler.py

Tarefas periódicas em background: backup automático do SQLite, limpeza
de memória/cache e um watchdog simples que monitora a saúde dos
serviços internos e tenta se recuperar sem reiniciar o bot inteiro.

Cada função é uma corrotina de loop infinito, pensada para ser
disparada com `asyncio.ensure_future` a partir de `on_ready`.
"""

from __future__ import annotations

import asyncio
import time

import discord

import logger as log
from ai import ProvedorDeIA
from config import Config
from database import Database
from memory import GerenciadorDeMemoria


async def loop_backup(db: Database, config: Config) -> None:
    """Cria backups do SQLite periodicamente, mantendo apenas os mais recentes."""
    intervalo_segundos = max(60, config.db_backup_intervalo_horas * 3600)
    while True:
        try:
            await db.criar_backup(manter_max=config.db_backup_max)
        except Exception as exc:  # noqa: BLE001
            log.erro("Falha ao criar backup automático", exc)
        await asyncio.sleep(intervalo_segundos)


async def loop_limpeza(memoria: GerenciadorDeMemoria, config: Config) -> None:
    """
    Remove periodicamente usuários inativos da memória em RAM, liberando
    recursos (a rotação de logs antigos já é feita automaticamente pelo
    TimedRotatingFileHandler, e backups excedentes são removidos em
    `criar_backup`)."""
    intervalo_segundos = max(60, config.limpeza_intervalo_horas * 3600)
    while True:
        try:
            removidos = memoria.limpar_inativos(config.memoria_inatividade_max_segundos)
            if removidos:
                log.limpeza(f"{removidos} usuário(s) inativo(s) removido(s) da memória em RAM.")
        except Exception as exc:  # noqa: BLE001
            log.erro("Falha na limpeza periódica de memória", exc)
        await asyncio.sleep(intervalo_segundos)


async def loop_watchdog(
    client: discord.Client,
    db: Database,
    ia: ProvedorDeIA,
    config: Config,
) -> None:
    """
    Monitor interno leve: verifica periodicamente se Discord, banco de
    dados e provedores de IA estão saudáveis. Em caso de problema,
    apenas registra no log e tenta uma recuperação simples (por exemplo,
    recarregar o cache do banco), sem nunca derrubar o processo.
    """
    while True:
        await asyncio.sleep(config.watchdog_intervalo_segundos)

        try:
            if client.is_closed():
                log.watchdog("Cliente Discord está fechado — aguardando reconexão automática do discord.py.")

            latencia = client.latency
            if latencia is not None and latencia > 5:
                log.watchdog(f"Latência alta com o Discord: {latencia * 1000:.0f}ms.")

            db_ok = await db.esta_saudavel()
            if not db_ok:
                log.watchdog("Banco de dados não respondeu à verificação de saúde.")

            status_provedores = ia.status_provedores()
            for provedor, status in status_provedores.items():
                if status == "indisponível":
                    log.watchdog(f"Provedor de IA '{provedor}' está indisponível (circuit breaker aberto).")

        except Exception as exc:  # noqa: BLE001 - o watchdog nunca pode derrubar o bot
            log.erro("Erro dentro do próprio watchdog (ignorado, watchdog continua rodando)", exc)


def iniciar_tarefas_em_background(
    client: discord.Client,
    db: Database,
    memoria: GerenciadorDeMemoria,
    ia: ProvedorDeIA,
    config: Config,
) -> list[asyncio.Task]:
    """Dispara todas as tarefas periódicas e retorna as referências das tasks."""
    tarefas = [
        asyncio.ensure_future(loop_backup(db, config)),
        asyncio.ensure_future(loop_limpeza(memoria, config)),
        asyncio.ensure_future(loop_watchdog(client, db, ia, config)),
    ]
    log.info("Tarefas em background iniciadas: backup, limpeza e watchdog.")
    return tarefas
