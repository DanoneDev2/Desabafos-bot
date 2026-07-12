"""
admin_panel.py

Painel administrativo enviado sob demanda (`/painel admin`), nunca
automaticamente — permite configurar a LORA quase inteiramente pelo
Discord, sem editar código, `.env` ou o banco manualmente.

Cada opção é persistida na tabela `configuracoes` (já existente em
`database.py`, reaproveitada em vez de duplicada) e lida em tempo real
por `ai.py` / `ticket_manager.py` / `events.py` através dos resolvedores
`valor_efetivo*` de `config.py`: não é preciso reiniciar o bot depois de
salvar. Um override salvo aqui tem prioridade sobre o `.env`; se nada
for salvo, o valor do `.env` continua valendo normalmente.

EXCEÇÃO EXPLÍCITA (regra do projeto, não apenas desta fase): os PROMPTS
da IA NUNCA aparecem aqui, nem são lidos do banco em lugar nenhum do
projeto — ficam exclusivamente em `prompts.py`. As API Keys continuam
exclusivamente no `.env`.

FASE 1 (v4.0): IA (modelo, temperatura) e Tickets (auto-close, limite).
FASE 2 (v4.x): Dashboard, Canais, Cargos (com múltiplos Helpers) e Crise
(tempo de espera + escalada automática), além do toggle de IA
ativa/desativada e da escolha entre threads privadas ou canais
tradicionais para os tickets.

Deliberadamente ainda fora de escopo (ver README, seção "Novidades"):
edição de todos os embeds do projeto campo a campo, painel de música
(playlists/upload de áudio) e transcripts em HTML. Cada um desses é,
sozinho, um recurso grande o bastante para merecer sua própria fase —
ver o raciocínio completo no README.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

import logger as log

if TYPE_CHECKING:
    from events import BotDeDesabafos


def montar_embed_admin() -> discord.Embed:
    """Embed do painel administrativo (Painel MAIN)."""
    return discord.Embed(
        title="⚙️ Painel MAIN — Administração da LORA",
        description=(
            "Configure a LORA sem editar código, `.env` ou o banco na mão.\n\n"
            "As alterações feitas aqui valem imediatamente (sem reiniciar o bot) "
            "e têm prioridade sobre o `.env`. Deixe um campo em branco no "
            "formulário para não alterar aquele valor.\n\n"
            "*Prompts da IA e API Keys não são configuráveis por aqui — ficam "
            "exclusivamente no código e no `.env`, por design.*"
        ),
        color=discord.Color.dark_teal(),
    ).set_footer(text="Apenas administradores deveriam ter acesso a este canal/comando.")


class PainelAdminView(discord.ui.View):
    """View persistente do Painel MAIN."""

    def __init__(self, bot: "BotDeDesabafos") -> None:
        super().__init__(timeout=None)
        self._bot = bot

    @discord.ui.button(label="📊 Dashboard", style=discord.ButtonStyle.green, custom_id="lora_admin_dashboard", row=0)
    async def botao_dashboard(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        embed = await self._bot.montar_embed_dashboard()
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="🤖 IA", style=discord.ButtonStyle.blurple, custom_id="lora_admin_ia", row=1)
    async def botao_ia(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(ModalConfigIA(self._bot))

    @discord.ui.button(label="🎫 Tickets", style=discord.ButtonStyle.blurple, custom_id="lora_admin_tickets", row=1)
    async def botao_tickets(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(ModalConfigTickets(self._bot))

    @discord.ui.button(label="📺 Canais", style=discord.ButtonStyle.blurple, custom_id="lora_admin_canais", row=1)
    async def botao_canais(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(ModalConfigCanais(self._bot))

    @discord.ui.button(label="👥 Cargos", style=discord.ButtonStyle.blurple, custom_id="lora_admin_cargos", row=2)
    async def botao_cargos(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(ModalConfigCargos(self._bot))

    @discord.ui.button(label="🚨 Crise", style=discord.ButtonStyle.red, custom_id="lora_admin_crise", row=2)
    async def botao_crise(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(ModalConfigCrise(self._bot))


def _texto_resultado(alteracoes: list[str]) -> str:
    return "\n".join(f"✅ {a}" for a in alteracoes) if alteracoes else "Nenhum campo preenchido — nada foi alterado."


def _normalizar_sim_nao(bruto: str) -> bool | None:
    """Converte 'sim'/'não'/'true'/'false' em bool; retorna None se inválido."""
    valor = bruto.strip().lower()
    if valor in ("sim", "true", "1"):
        return True
    if valor in ("não", "nao", "false", "0"):
        return False
    return None


class ModalConfigIA(discord.ui.Modal, title="Configurações de IA"):
    """
    Ajusta temperatura, modelos e o interruptor geral da IA, com efeito
    imediato. Os PROMPTS da IA nunca aparecem aqui — ficam exclusivamente
    em `prompts.py`, por regra explícita do projeto (ver docstring do módulo).
    """

    temperatura: discord.ui.TextInput = discord.ui.TextInput(
        label="Temperatura (0.0 a 2.0)", required=False, placeholder="ex: 0.9", max_length=5
    )
    modelo_gemini: discord.ui.TextInput = discord.ui.TextInput(
        label="Modelo Gemini", required=False, placeholder="ex: gemini-1.5-flash", max_length=100
    )
    modelo_groq: discord.ui.TextInput = discord.ui.TextInput(
        label="Modelo Groq", required=False, placeholder="ex: llama-3.1-70b-versatile", max_length=100
    )
    ia_ativa: discord.ui.TextInput = discord.ui.TextInput(
        label="IA ativa? (sim/não, em branco = não altera)", required=False, placeholder="sim", max_length=5
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

        if self.ia_ativa.value.strip():
            ativa = _normalizar_sim_nao(self.ia_ativa.value)
            if ativa is None:
                await interaction.response.send_message(
                    "Valor inválido para 'IA ativa' (use sim/não) — nada foi salvo.", ephemeral=True
                )
                return
            await db.definir_configuracao("ia_ativa", "true" if ativa else "false")
            alteracoes.append(f"IA ativa → `{'sim' if ativa else 'não'}`")

        for alteracao in alteracoes:
            log.configuracao_alterada(alteracao, str(interaction.user))
        await interaction.response.send_message(_texto_resultado(alteracoes), ephemeral=True)


class ModalConfigTickets(discord.ui.Modal, title="Configurações de Tickets"):
    """Ajusta auto-close, limite de sessões e o tipo de canal usado nos tickets."""

    auto_close_horas: discord.ui.TextInput = discord.ui.TextInput(
        label="Horas de inatividade p/ avisar", required=False, placeholder="ex: 48", max_length=6
    )
    session_limit: discord.ui.TextInput = discord.ui.TextInput(
        label="Limite de sessões abertas (0 = sem limite)", required=False, placeholder="ex: 0", max_length=6
    )
    usar_threads: discord.ui.TextInput = discord.ui.TextInput(
        label="Usar threads privadas? (sim/não)", required=False, placeholder="não", max_length=5
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

        if self.usar_threads.value.strip():
            usar = _normalizar_sim_nao(self.usar_threads.value)
            if usar is None:
                await interaction.response.send_message(
                    "Valor inválido para 'Usar threads' (use sim/não) — nada foi salvo.", ephemeral=True
                )
                return
            await db.definir_configuracao("enable_private_threads", "true" if usar else "false")
            alteracoes.append(f"Tickets como → `{'threads privadas' if usar else 'canais tradicionais'}`")
            alteracoes.append("⚠️ só vale para tickets criados a partir de agora")

        for alteracao in alteracoes:
            log.configuracao_alterada(alteracao, str(interaction.user))
        await interaction.response.send_message(_texto_resultado(alteracoes), ephemeral=True)


class ModalConfigCanais(discord.ui.Modal, title="Canais dedicados"):
    """Configura os canais usados pelo bot para transcripts, logs, alertas e crises graves."""

    canal_transcripts: discord.ui.TextInput = discord.ui.TextInput(
        label="ID do canal de transcripts", required=False, placeholder="ex: 123456789012345678", max_length=25
    )
    canal_logs: discord.ui.TextInput = discord.ui.TextInput(
        label="ID do canal de logs", required=False, placeholder="ex: 123456789012345678", max_length=25
    )
    canal_alertas: discord.ui.TextInput = discord.ui.TextInput(
        label="ID do canal de alertas", required=False, placeholder="ex: 123456789012345678", max_length=25
    )
    canal_crise_grave: discord.ui.TextInput = discord.ui.TextInput(
        label="ID do canal de crise grave", required=False, placeholder="ex: 123456789012345678", max_length=25
    )
    canal_chamada_helpers: discord.ui.TextInput = discord.ui.TextInput(
        label="ID do canal de chamada de Helpers", required=False, placeholder="ex: 123456789012345678", max_length=25
    )

    def __init__(self, bot: "BotDeDesabafos") -> None:
        super().__init__()
        self._bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        db = self._bot.db
        alteracoes: list[str] = []
        campos = {
            "canal_transcripts": (self.canal_transcripts, "Canal de transcripts"),
            "canal_logs": (self.canal_logs, "Canal de logs"),
            "canal_alertas": (self.canal_alertas, "Canal de alertas"),
            "canal_crise_grave": (self.canal_crise_grave, "Canal de crise grave"),
            "canal_chamada_helpers": (self.canal_chamada_helpers, "Canal de chamada de Helpers"),
        }

        for chave, (campo, rotulo) in campos.items():
            if not campo.value.strip():
                continue
            if not campo.value.strip().isdigit():
                await interaction.response.send_message(
                    f"'{rotulo}' precisa ser o ID numérico do canal — nada foi salvo.", ephemeral=True
                )
                return
            await db.definir_configuracao(chave, campo.value.strip())
            alteracoes.append(f"{rotulo} → `{campo.value.strip()}`")

        for alteracao in alteracoes:
            log.configuracao_alterada(alteracao, str(interaction.user))
        await interaction.response.send_message(_texto_resultado(alteracoes), ephemeral=True)


class ModalConfigCargos(discord.ui.Modal, title="Cargos"):
    """
    Configura os cargos de Staff, Helper(s) (múltiplos, separados por
    vírgula) e Supervisor.
    """

    staff: discord.ui.TextInput = discord.ui.TextInput(
        label="ID do cargo Staff", required=False, placeholder="ex: 123456789012345678", max_length=25
    )
    helpers: discord.ui.TextInput = discord.ui.TextInput(
        label="IDs dos cargos Helper (separados por vírgula)",
        required=False,
        placeholder="ex: 111,222,333",
        max_length=200,
    )
    supervisor: discord.ui.TextInput = discord.ui.TextInput(
        label="ID do cargo Supervisor", required=False, placeholder="ex: 123456789012345678", max_length=25
    )

    def __init__(self, bot: "BotDeDesabafos") -> None:
        super().__init__()
        self._bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        db = self._bot.db
        alteracoes: list[str] = []

        if self.staff.value.strip():
            if not self.staff.value.strip().isdigit():
                await interaction.response.send_message("ID do cargo Staff inválido — nada foi salvo.", ephemeral=True)
                return
            await db.definir_configuracao("staff_role_id", self.staff.value.strip())
            alteracoes.append(f"Cargo Staff → `{self.staff.value.strip()}`")

        if self.helpers.value.strip():
            partes = [p.strip() for p in self.helpers.value.split(",") if p.strip()]
            if not all(p.isdigit() for p in partes):
                await interaction.response.send_message(
                    "Os IDs dos cargos Helper devem ser números separados por vírgula — nada foi salvo.",
                    ephemeral=True,
                )
                return
            # Junta com o cargo Staff (se configurado) numa única lista de "cargos de apoio".
            staff_atual = db.obter_configuracao("staff_role_id") or str(self._bot.config.staff_role_id or "")
            todos = partes + ([staff_atual] if staff_atual else [])
            await db.definir_configuracao("cargos_apoio_ids", ",".join(todos))
            alteracoes.append(f"Cargos Helper → `{', '.join(partes)}` ({len(partes)} cargo(s))")

        if self.supervisor.value.strip():
            if not self.supervisor.value.strip().isdigit():
                await interaction.response.send_message(
                    "ID do cargo Supervisor inválido — nada foi salvo.", ephemeral=True
                )
                return
            await db.definir_configuracao("supervisor_role_id", self.supervisor.value.strip())
            alteracoes.append(f"Cargo Supervisor → `{self.supervisor.value.strip()}`")

        for alteracao in alteracoes:
            log.configuracao_alterada(alteracao, str(interaction.user))
        await interaction.response.send_message(_texto_resultado(alteracoes), ephemeral=True)


class ModalConfigCrise(discord.ui.Modal, title="Configurações de Crise"):
    """Configura a detecção e a escalada automática de crise."""

    ativa: discord.ui.TextInput = discord.ui.TextInput(
        label="Detecção de crise ativa? (sim/não)", required=False, placeholder="sim", max_length=5
    )
    tempo_maximo_espera: discord.ui.TextInput = discord.ui.TextInput(
        label="Minutos de espera por um Helper", required=False, placeholder="ex: 15", max_length=6
    )
    escalada_automatica: discord.ui.TextInput = discord.ui.TextInput(
        label="Escalada automática ativa? (sim/não)", required=False, placeholder="sim", max_length=5
    )

    def __init__(self, bot: "BotDeDesabafos") -> None:
        super().__init__()
        self._bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        db = self._bot.db
        alteracoes: list[str] = []

        if self.ativa.value.strip():
            ativa = _normalizar_sim_nao(self.ativa.value)
            if ativa is None:
                await interaction.response.send_message("Valor inválido (use sim/não) — nada foi salvo.", ephemeral=True)
                return
            await db.definir_configuracao("enable_crisis_mode", "true" if ativa else "false")
            alteracoes.append(f"Detecção de crise → `{'ativa' if ativa else 'inativa'}`")

        if self.tempo_maximo_espera.value.strip():
            try:
                minutos = int(self.tempo_maximo_espera.value.strip())
            except ValueError:
                await interaction.response.send_message("Valor inválido para minutos — nada foi salvo.", ephemeral=True)
                return
            await db.definir_configuracao("crise_tempo_maximo_espera_helper_minutos", str(minutos))
            alteracoes.append(f"Tempo máximo de espera por Helper → `{minutos} min`")

        if self.escalada_automatica.value.strip():
            escalada = _normalizar_sim_nao(self.escalada_automatica.value)
            if escalada is None:
                await interaction.response.send_message("Valor inválido (use sim/não) — nada foi salvo.", ephemeral=True)
                return
            await db.definir_configuracao("crise_escalada_automatica", "true" if escalada else "false")
            alteracoes.append(f"Escalada automática → `{'ativa' if escalada else 'inativa'}`")

        for alteracao in alteracoes:
            log.configuracao_alterada(alteracao, str(interaction.user))
        await interaction.response.send_message(_texto_resultado(alteracoes), ephemeral=True)
