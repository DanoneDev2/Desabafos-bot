"""
ui.py

Componentes de interface (Views/Botões) do sistema de tickets. Este
módulo é propositalmente "burro": não decide regras de negócio, apenas
captura a interação da pessoa e delega para métodos do bot
(`events.BotDeDesabafos`), que por sua vez usam `ticket_manager.py`,
`database.py` e `summarizer.py`.

Todos os botões usam `custom_id` fixo (sem `session_id` embutido) e
`timeout=None`, o que os torna persistentes — continuam funcionando
após o bot reiniciar, desde que `bot.add_view(...)` seja chamado uma
vez em `setup_hook`. A sessão correspondente é sempre localizada pelo
canal em que o botão foi clicado (`interaction.channel.id`), nunca
codificada no botão em si — assim, uma única instância de cada View
cobre todos os tickets, presentes e futuros.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from events import BotDeDesabafos


class PainelView(discord.ui.View):
    """Painel permanente com o botão para iniciar uma nova conversa privada."""

    def __init__(self, bot: "BotDeDesabafos") -> None:
        super().__init__(timeout=None)
        self._bot = bot

    @discord.ui.button(
        label="Iniciar Conversa",
        emoji="💜",
        style=discord.ButtonStyle.blurple,
        custom_id="lora:iniciar_conversa",
    )
    async def iniciar_conversa(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._bot.iniciar_conversa(interaction)


class TicketView(discord.ui.View):
    """View fixada no canal do ticket, com o botão de encerrar a conversa."""

    def __init__(self, bot: "BotDeDesabafos") -> None:
        super().__init__(timeout=None)
        self._bot = bot

    @discord.ui.button(
        label="Encerrar Conversa",
        emoji="🔒",
        style=discord.ButtonStyle.red,
        custom_id="lora:encerrar_conversa",
    )
    async def encerrar_conversa(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._bot.solicitar_confirmacao_encerramento(interaction)


class ConfirmarEncerramentoView(discord.ui.View):
    """Confirmação antes de encerrar de fato uma sessão."""

    def __init__(self, bot: "BotDeDesabafos") -> None:
        super().__init__(timeout=None)
        self._bot = bot

    @discord.ui.button(
        label="Sim, encerrar",
        emoji="✅",
        style=discord.ButtonStyle.red,
        custom_id="lora:confirmar_encerrar",
    )
    async def confirmar(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._bot.confirmar_encerramento(interaction)

    @discord.ui.button(
        label="Cancelar",
        emoji="↩️",
        style=discord.ButtonStyle.grey,
        custom_id="lora:cancelar_encerrar",
    )
    async def cancelar(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Ok, vamos continuar conversando. 💜", view=None)


class AvisoInatividadeView(discord.ui.View):
    """Enviada automaticamente após um período de inatividade, perguntando se pode encerrar."""

    def __init__(self, bot: "BotDeDesabafos") -> None:
        super().__init__(timeout=None)
        self._bot = bot

    @discord.ui.button(
        label="Continuar conversa",
        emoji="💬",
        style=discord.ButtonStyle.blurple,
        custom_id="lora:continuar_conversa",
    )
    async def continuar(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._bot.continuar_apos_aviso(interaction)

    @discord.ui.button(
        label="Encerrar agora",
        emoji="🔒",
        style=discord.ButtonStyle.red,
        custom_id="lora:encerrar_agora",
    )
    async def encerrar_agora(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._bot.confirmar_encerramento(interaction)


def montar_embed_painel() -> discord.Embed:
    """Monta o embed do painel principal ('🌙 LORA')."""
    return discord.Embed(
        title="🌙 LORA",
        description=(
            "Você não precisa enfrentar tudo sozinho(a).\n\n"
            "Este espaço foi criado para que você possa conversar de forma "
            "privada, no seu tempo, sobre o que estiver sentindo.\n\n"
            "Clique abaixo para iniciar uma conversa."
        ),
        color=discord.Color.dark_purple(),
    )
