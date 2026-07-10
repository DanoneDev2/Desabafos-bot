"""
admin_panel.py

Painel administrativo enviado sob demanda (`/painel admin`), nunca
automaticamente — permite configurar a LORA inteiramente pelo Discord,
sem editar código, `.env` ou o banco manualmente.

Cada opção é persistida na tabela `configuracoes` (já existente em
`database.py`, reaproveitada em vez de duplicada) e lida em tempo real
por `ai.py` / `ticket_manager.py`: não é preciso reiniciar o bot depois
de salvar. Um override salvo aqui tem prioridade sobre o `.env`; se
nada for salvo, o valor do `.env` continua valendo normalmente.

FASE 1 (v4.0): cobre os ajustes de maior impacto no dia a dia — IA
(modelo, temperatura, prompt geral) e Tickets (auto-close, limite de
sessões). O painel foi projetado para crescer: cada nova seção é só um
botão a mais + um novo Modal, sem tocar no que já existe aqui.

Deliberadamente fora desta fase (ver README, seção "Novidades da v4.0"
para o raciocínio): edição de todos os embeds do projeto, painel de
música, base de conhecimento e dashboard em tempo real.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from events import BotDeDesabafos


def montar_embed_admin() -> discord.Embed:
    """Embed do painel administrativo."""
    return discord.Embed(
        title="⚙️ Painel Administrativo — LORA",
        description=(
            "Configure a LORA sem editar código, `.env` ou o banco na mão.\n\n"
            "As alterações feitas aqui valem imediatamente (sem reiniciar o bot) "
            "e têm prioridade sobre o `.env`. Deixe um campo em branco no "
            "formulário para não alterar aquele valor."
        ),
        color=discord.Color.dark_teal(),
    ).set_footer(text="Apenas administradores deveriam ter acesso a este canal/comando.")


class PainelAdminView(discord.ui.View):
    """View persistente do painel administrativo."""

    def __init__(self, bot: "BotDeDesabafos") -> None:
        super().__init__(timeout=None)
        self._bot = bot

    @discord.ui.button(label="🤖 IA", style=discord.ButtonStyle.blurple, custom_id="lora_admin_ia")
    async def botao_ia(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(ModalConfigIA(self._bot))

    @discord.ui.button(label="🎫 Tickets", style=discord.ButtonStyle.blurple, custom_id="lora_admin_tickets")
    async def botao_tickets(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(ModalConfigTickets(self._bot))


class ModalConfigIA(discord.ui.Modal, title="Configurações de IA"):
    """Ajusta temperatura, modelos e o prompt geral, com efeito imediato."""

    temperatura: discord.ui.TextInput = discord.ui.TextInput(
        label="Temperatura (0.0 a 2.0)", required=False, placeholder="ex: 0.9", max_length=5
    )
    modelo_gemini: discord.ui.TextInput = discord.ui.TextInput(
        label="Modelo Gemini", required=False, placeholder="ex: gemini-1.5-flash", max_length=100
    )
    modelo_groq: discord.ui.TextInput = discord.ui.TextInput(
        label="Modelo Groq", required=False, placeholder="ex: llama-3.1-70b-versatile", max_length=100
    )
    prompt_geral: discord.ui.TextInput = discord.ui.TextInput(
        label="Prompt geral (em branco = não altera)",
        required=False,
        style=discord.TextStyle.paragraph,
        max_length=4000,
    )

    def __init__(self, bot: "BotDeDesabafos") -> None:
        super().__init__()
        self._bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        db = self._bot.db
        alteracoes: list[str] = []

        if self.temperatura.value.strip():
            try:
                valor = float(self.temperatura.value.strip())
            except ValueError:
                await interaction.response.send_message("Temperatura inválida — nada foi salvo.", ephemeral=True)
                return
            await db.definir_configuracao("ia_temperature", str(valor))
            alteracoes.append(f"Temperatura → `{valor}`")

        if self.modelo_gemini.value.strip():
            await db.definir_configuracao("ia_model_gemini", self.modelo_gemini.value.strip())
            alteracoes.append(f"Modelo Gemini → `{self.modelo_gemini.value.strip()}`")

        if self.modelo_groq.value.strip():
            await db.definir_configuracao("ia_model_groq", self.modelo_groq.value.strip())
            alteracoes.append(f"Modelo Groq → `{self.modelo_groq.value.strip()}`")

        if self.prompt_geral.value.strip():
            await db.definir_configuracao("prompt_geral", self.prompt_geral.value.strip())
            alteracoes.append("Prompt geral atualizado")

        texto = "\n".join(f"✅ {a}" for a in alteracoes) if alteracoes else "Nenhum campo preenchido — nada foi alterado."
        await interaction.response.send_message(texto, ephemeral=True)


class ModalConfigTickets(discord.ui.Modal, title="Configurações de Tickets"):
    """Ajusta auto-close e limite de sessões, com efeito imediato."""

    auto_close_horas: discord.ui.TextInput = discord.ui.TextInput(
        label="Horas de inatividade p/ avisar", required=False, placeholder="ex: 48", max_length=6
    )
    session_limit: discord.ui.TextInput = discord.ui.TextInput(
        label="Limite de sessões abertas (0 = sem limite)", required=False, placeholder="ex: 0", max_length=6
    )

    def __init__(self, bot: "BotDeDesabafos") -> None:
        super().__init__()
        self._bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        db = self._bot.db
        alteracoes: list[str] = []

        if self.auto_close_horas.value.strip():
            try:
                valor = float(self.auto_close_horas.value.strip())
            except ValueError:
                await interaction.response.send_message("Valor inválido para horas — nada foi salvo.", ephemeral=True)
                return
            await db.definir_configuracao("auto_close_horas", str(valor))
            alteracoes.append(f"Auto-close → `{valor}h`")

        if self.session_limit.value.strip():
            try:
                valor_int = int(self.session_limit.value.strip())
            except ValueError:
                await interaction.response.send_message("Valor inválido para limite — nada foi salvo.", ephemeral=True)
                return
            await db.definir_configuracao("session_limit", str(valor_int))
            alteracoes.append(f"Limite de sessões → `{valor_int}`")

        texto = "\n".join(f"✅ {a}" for a in alteracoes) if alteracoes else "Nenhum campo preenchido — nada foi alterado."
        await interaction.response.send_message(texto, ephemeral=True)
