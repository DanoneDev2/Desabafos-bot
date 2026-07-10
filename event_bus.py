"""
event_bus.py

Barramento de eventos interno, simples e sem dependências externas.
Permite que módulos futuros (música, timeline, analytics, base de
conhecimento, etc.) reajam a eventos do bot sem que os módulos
existentes precisem conhecê-los — cada novo "plugin" apenas se
inscreve nos eventos que lhe interessam.

Eventos emitidos atualmente pelo projeto (nomes estáveis, use-os ao
criar novos ouvintes):

    sessao_criada        (session_id, user_id, channel_id)
    sessao_encerrada      (session_id, user_id, resumo)
    crise_detectada       (session_id)
    resumo_criado         (session_id, modelo)
    helper_entrou         (session_id, helper_id)
    ia_retomada           (session_id)
    avaliacao_recebida    (session_id, user_id, estrelas)

Este módulo não sabe nada sobre Discord, IA ou banco de dados — apenas
distribui eventos para quem se inscreveu.
"""

from __future__ import annotations

import inspect
from collections import defaultdict
from typing import Any, Awaitable, Callable, Union

import logger as log

_Ouvinte = Callable[..., Union[None, Awaitable[None]]]


class EventBus:
    """Barramento de eventos simples (padrão publish/subscribe) em memória."""

    def __init__(self) -> None:
        self._ouvintes: dict[str, list[_Ouvinte]] = defaultdict(list)

    def on(self, evento: str, callback: _Ouvinte) -> None:
        """Inscreve um callback (síncrono ou assíncrono) para um evento."""
        self._ouvintes[evento].append(callback)

    def remover(self, evento: str, callback: _Ouvinte) -> None:
        """Remove a inscrição de um callback, se existir."""
        if callback in self._ouvintes.get(evento, []):
            self._ouvintes[evento].remove(callback)

    async def emit(self, evento: str, **dados: Any) -> None:
        """
        Notifica todos os ouvintes inscritos em `evento`. Falhas em um
        ouvinte são registradas no log e nunca derrubam o bot nem afetam
        os demais ouvintes.
        """
        for callback in list(self._ouvintes.get(evento, [])):
            try:
                resultado = callback(**dados)
                if inspect.isawaitable(resultado):
                    await resultado
            except Exception as exc:  # noqa: BLE001
                log.erro(f"Falha em um ouvinte do evento '{evento}'", exc)


# Instância única e compartilhada por todo o projeto — módulos futuros
# podem simplesmente `from event_bus import bus` e usar `bus.on(...)`.
bus = EventBus()
