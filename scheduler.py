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
from datetime import datetime, timezone

import discord

import logger as log
import ui
from ai import ProvedorDeIA
from config import Config
from database import Database
from memory import GerenciadorDeMemoria
from ticket_manager import GerenciadorDeTickets


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


async def loop_fechamento_automatico(
    client: discord.Client,
    tickets: GerenciadorDeTickets,
    ia: ProvedorDeIA,
    config: Config,
) -> None:
    """
    Verifica periodicamente os tickets abertos: envia um aviso de
    inatividade após `AUTO_CLOSE_HOURS` sem mensagens e, se a sessão
    continuar inativa por mais `AUTO_CLOSE_TOLERANCIA_HORAS`, encerra a
    conversa automaticamente (gerando o resumo normalmente).
    """
    intervalo_segundos = max(60, config.ticket_check_intervalo_minutos * 60)
    while True:
        try:
            sessoes = await tickets.sessoes_para_avaliar_fechamento()
            agora = datetime.now(timezone.utc)

            for sessao in sessoes:
                if not sessao.last_activity:
                    continue

                canal = client.get_channel(sessao.channel_id)
                if canal is None:
                    continue  # canal já não existe mais (ex: apagado manualmente)

                try:
                    ultima_atividade = datetime.fromisoformat(sessao.last_activity)
                except ValueError:
                    continue

                horas_inativa = (agora - ultima_atividade).total_seconds() / 3600

                if not sessao.aviso_inatividade_enviado and horas_inativa >= config.auto_close_horas:
                    try:
                        await canal.send(
                            "Faz um tempo que a gente não conversa por aqui. Posso encerrar esta "
                            "conversa? Se preferir continuar, é só me responder. 💜",
                            view=ui.AvisoInatividadeView(client),
                        )
                        await tickets.marcar_aviso_inatividade(sessao.id)
                    except Exception as exc:  # noqa: BLE001
                        log.erro(f"Falha ao enviar aviso de inatividade da sessão #{sessao.id}", exc)

                elif sessao.aviso_inatividade_enviado and horas_inativa >= (
                    config.auto_close_horas + config.auto_close_tolerancia_horas
                ):
                    try:
                        await tickets.encerrar_definitivamente(sessao, canal, ia, config)
                    except Exception as exc:  # noqa: BLE001
                        log.erro(f"Falha ao encerrar automaticamente a sessão #{sessao.id}", exc)

        except Exception as exc:  # noqa: BLE001 - o loop nunca pode morrer
            log.erro("Falha no loop de fechamento automático de tickets", exc)

        await asyncio.sleep(intervalo_segundos)


def iniciar_tarefas_em_background(
    client: discord.Client,
    db: Database,
    memoria: GerenciadorDeMemoria,
    ia: ProvedorDeIA,
    config: Config,
    tickets: GerenciadorDeTickets,
) -> list[asyncio.Task]:
    """Dispara todas as tarefas periódicas e retorna as referências das tasks."""
    tarefas = [
        asyncio.ensure_future(loop_backup(db, config)),
        asyncio.ensure_future(loop_limpeza(memoria, config)),
        asyncio.ensure_future(loop_watchdog(client, db, ia, config)),
        asyncio.ensure_future(loop_fechamento_automatico(client, tickets, ia, config)),
    ]
    log.info("Tarefas em background iniciadas: backup, limpeza, watchdog e fechamento automático de tickets.")
    return tarefas
