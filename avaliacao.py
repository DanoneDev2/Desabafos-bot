"""
avaliacao.py

Sistema de avaliação pós-atendimento: ao encerrar uma sessão, a pessoa
recebe (por DM, já que o canal do ticket é apagado) um convite para
avaliar de 1 a 5 estrelas, com um comentário opcional. Persistido via
`database.py` (tabela `avaliacoes`) — nenhum SQL fora de `database.py`.

FEEDBACK PARA IA (v4.0): avaliações de 1 ou 2 estrelas incrementam a
estatística `avaliacoes_negativas` automaticamente (em
`Database.salvar_avaliacao`). Isso já é suficiente para identificar,
via `/health` ou consulta direta ao banco, se uma proporção alta de
sessões está sendo mal avaliada — uma base simples e honesta para uma
futura análise mais fina (qual resposta, contexto e emoção estavam
envolvidos), sem inventar uma automação que ainda não existe.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from event_bus import bus

if TYPE_CHECKING:
    from database import Database

_TRES_DIAS_EM_SEGUNDOS = 60 * 60 * 24 * 3


def montar_embed_avaliacao() -> discord.Embed:
    """Embed enviado por DM ao final de uma sessão, convidando para avaliar."""
    return discord.Embed(
        title="💜 Como foi nossa conversa?",
        description="Sua avaliação é anônima para outros usuários e ajuda a melhorar o atendimento.",
        color=discord.Color.gold(),
    )


class ModalComentario(discord.ui.Modal, title="Deixe um comentário (opcional)"):
    """Comentário livre associado à nota já escolhida nos botões de estrela."""

    comentario: discord.ui.TextInput = discord.ui.TextInput(
        label="O que você achou da conversa?",
        required=False,
        style=discord.TextStyle.paragraph,
        max_length=500,
    )

    def __init__(
        self,
        db: "Database",
        session_id: int,
        user_id: int,
        estrelas: int,
        mensagem: discord.Message | None,
        view_pai: "AvaliacaoView",
    ) -> None:
        super().__init__()
        self._db = db
        self._session_id = session_id
        self._user_id = user_id
        self._estrelas = estrelas
        self._mensagem = mensagem
        self._view_pai = view_pai

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._db.salvar_avaliacao(
            self._session_id, self._user_id, self._estrelas, self.comentario.value.strip() or None
        )
        await bus.emit(
            "avaliacao_recebida", session_id=self._session_id, user_id=self._user_id, estrelas=self._estrelas
        )

        for item in self._view_pai.children:
            item.disabled = True
        if self._mensagem is not None:
            try:
                await self._mensagem.edit(view=self._view_pai)
            except discord.HTTPException:
                pass

        await interaction.response.send_message("Obrigado pela sua avaliação! 💜", ephemeral=True)


class _BotaoEstrela(discord.ui.Button):
    """Um botão de 1 a 5 estrelas."""

    def __init__(self, estrelas: int, view_pai: "AvaliacaoView") -> None:
        super().__init__(label="⭐" * estrelas, style=discord.ButtonStyle.grey)
        self._estrelas = estrelas
        self._view_pai = view_pai

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            ModalComentario(
                self._view_pai.db,
                self._view_pai.session_id,
                self._view_pai.user_id,
                self._estrelas,
                interaction.message,
                self._view_pai,
            )
        )


class AvaliacaoView(discord.ui.View):
    """
    View com botões de 1 a 5 estrelas. Não é persistente entre reinícios
    do bot (não há necessidade: é enviada e respondida logo após o
    encerramento de uma sessão, com validade de alguns dias).
    """

    def __init__(self, db: "Database", session_id: int, user_id: int) -> None:
        super().__init__(timeout=_TRES_DIAS_EM_SEGUNDOS)
        self.db = db
        self.session_id = session_id
        self.user_id = user_id
        for n in range(1, 6):
            self.add_item(_BotaoEstrela(n, self))
