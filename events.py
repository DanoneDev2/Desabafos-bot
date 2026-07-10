"""
events.py

Contém a classe do bot Discord e o tratamento de eventos (on_ready,
on_message, interações de botão, etc). Toda a lógica de negócio é
delegada a outros módulos — este arquivo apenas orquestra:

    - ticket_manager.py: ciclo de vida das sessões privadas (tickets);
    - ui.py: painel e botões (Iniciar Conversa / Encerrar Conversa);
    - crisis.py: detecção determinística de risco à vida;
    - emotion.py: classificação leve do tom emocional de cada mensagem;
    - summarizer.py: resumo estruturado gerado ao encerrar uma sessão;
    - ai.py / memory.py / database.py: já existentes, preservados.

ATUALIZAÇÃO v3.0 — de um único canal compartilhado para conversas
privadas por sessão (tickets): cada usuário passa a ter seu próprio
canal privado, criado sob demanda a partir de um painel permanente.
"""

from __future__ import annotations

import os
import time

import discord
from discord import app_commands

import crisis
import emotion
import logger as log
import scheduler
import ui
import version
from admin_panel import PainelAdminView, montar_embed_admin
from ai import ErroDeIA, ProvedorDeIA
from config import Config
from database import Database
from memory import GerenciadorDeMemoria
from prompts import PROMPT_COOPERACAO
from ticket_manager import GerenciadorDeTickets, Sessao
from utils import ControladorDeCooldown, mensagem_valida, uso_memoria_mb


class BotDeDesabafos(discord.Client):
    """
    Cliente Discord do LORA: publica o painel de "Iniciar Conversa",
    gerencia sessões privadas (tickets) por usuário e delega a geração
    de respostas para um ProvedorDeIA compartilhado.
    """

    def __init__(self, config: Config, db: Database, **kwargs) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents, **kwargs)

        self._config = config
        self._db = db
        self._memoria = GerenciadorDeMemoria(
            max_history=config.max_history,
            contexto_maximo_envio=config.contexto_efetivo(),
            resumo_max_caracteres=config.resumo_max_caracteres,
        )
        self._tickets = GerenciadorDeTickets(db, config, memoria=self._memoria)
        self._cooldown = ControladorDeCooldown(segundos=config.cooldown_segundos)
        self._ia = ProvedorDeIA(
            config, memoria=self._memoria, registrar_estatistica=self._registrar_estatistica, db=self._db
        )

        self._tentativas_reconexao = 0
        self._inicio_bot = time.monotonic()
        self._soma_tempo_resposta_ms = 0.0
        self._contagem_respostas = 0
        self._tarefas_background: list = []

        self.tree = app_commands.CommandTree(self)
        self._registrar_comandos()

    async def _registrar_estatistica(self, chave: str) -> None:
        """Callback repassado ao ProvedorDeIA para persistir estatísticas por provedor."""
        await self._db.incrementar_estatistica(chave)

    @property
    def db(self) -> Database:
        """Acesso à camada de persistência, para módulos que gerenciam sua própria configuração (ex: admin_panel.py)."""
        return self._db

    # ------------------------------------------------------------------
    # Ciclo de vida
    # ------------------------------------------------------------------

    async def setup_hook(self) -> None:
        """
        Executado pelo discord.py dentro do event loop, antes de conectar
        totalmente. Inicializa o banco (migração automática incluída) e
        registra as Views persistentes, para que os botões do painel e
        dos tickets continuem funcionando mesmo após o bot reiniciar.
        """
        await self._db.iniciar()

        self.add_view(ui.PainelView(self))
        self.add_view(ui.TicketView(self))
        self.add_view(ui.ConfirmarEncerramentoView(self))
        self.add_view(ui.AvisoInatividadeView(self))
        self.add_view(PainelAdminView(self))

    async def close(self) -> None:
        """Encerra a conexão com o banco de dados antes de desligar o bot."""
        await self._db.fechar()
        await super().close()

    # ------------------------------------------------------------------
    # Eventos do Discord
    # ------------------------------------------------------------------

    async def on_ready(self) -> None:
        """Disparado quando o bot conecta com sucesso ao Discord."""
        self._tentativas_reconexao = 0
        nome = str(self.user) if self.user else "desconhecido"
        log.bot_conectado(nome, len(self.guilds))

        try:
            await self.tree.sync()
        except discord.HTTPException as exc:
            log.erro("Falha ao sincronizar comandos slash", exc)

        # v4.0: o painel deixa de ser enviado automaticamente. Use
        # `/painel enviar` (ou `/painel admin` para o painel de
        # configuração) sempre que quiser publicá-lo ou republicá-lo.

        if not self._tarefas_background:
            self._tarefas_background = scheduler.iniciar_tarefas_em_background(
                client=self,
                db=self._db,
                memoria=self._memoria,
                ia=self._ia,
                config=self._config,
                tickets=self._tickets,
            )

    async def on_disconnect(self) -> None:
        """Disparado quando a conexão com o Discord cai."""
        self._tentativas_reconexao += 1
        log.reconexao(self._tentativas_reconexao)

    async def _garantir_painel(self, forcar: bool = False) -> bool:
        """
        Publica o painel permanente ('Iniciar Conversa') no canal
        configurado. Por padrão, não publica de novo se já existir um
        (o id da mensagem fica salvo no SQLite); `forcar=True` sempre
        publica um novo, para uso pelo comando `/painel enviar`.

        Retorna True se um painel foi publicado (ou já existia), e
        False se não foi possível (canal ausente ou erro de permissão).
        """
        canal_id = self._config.canal_painel
        if not canal_id:
            return False

        canal = self.get_channel(canal_id)
        if canal is None:
            log.aviso("Canal do painel não encontrado; verifique a variável CANAL_DESABAFOS.")
            return False

        if not forcar:
            mensagem_id_salva = self._db.obter_configuracao("painel_mensagem_id")
            if mensagem_id_salva:
                try:
                    await canal.fetch_message(int(mensagem_id_salva))
                    return True  # o painel já existe, nada a fazer
                except discord.NotFound:
                    pass  # a mensagem foi apagada; publica uma nova abaixo
                except (discord.HTTPException, ValueError) as exc:
                    log.erro("Falha ao verificar o painel existente", exc)
                    return False

        try:
            mensagem = await canal.send(embed=ui.montar_embed_painel(), view=ui.PainelView(self))
            await self._db.definir_configuracao("painel_mensagem_id", str(mensagem.id))
            log.painel_enviado("principal", canal.name)
            return True
        except discord.HTTPException as exc:
            log.erro("Falha ao publicar o painel", exc)
            return False

    async def on_message(self, message: discord.Message) -> None:
        """
        Processa mensagens apenas dentro de canais de ticket (sessões
        privadas) ativos. Mensagens em qualquer outro canal (incluindo o
        canal do painel) são ignoradas.

        v4.0 — cada sessão tem um `modo`:
            - "ia": fluxo normal, a IA responde diretamente à pessoa;
            - "observador": um Helper humano assumiu; a IA só registra,
              nunca responde;
            - "cooperacao": a IA sugere uma resposta, mas apenas o
              Helper a vê (por DM) — nunca é enviada à pessoa.

        Uma mensagem de um membro com cargo de Staff/Helper dentro do
        ticket, enquanto o modo ainda é "ia", pausa a IA automaticamente
        (equivalente a rodar `/ia pausar`).
        """
        if message.author.bot:
            return

        sessao = await self._tickets.sessao_por_canal(message.channel.id)
        if sessao is None:
            return

        if message.author.id != sessao.user_id:
            await self._processar_mensagem_de_apoio(message, sessao)
            return

        await self._processar_mensagem_do_usuario(message, sessao)

    async def _processar_mensagem_de_apoio(self, message: discord.Message, sessao) -> None:
        """
        Mensagem de alguém que não é o dono do ticket (Staff/Helper,
        já que o canal só é visível para eles + o próprio usuário). Se
        a sessão ainda estiver em Modo IA, assumir pausa automaticamente.
        """
        if not self._eh_membro_de_apoio(message.author):
            return  # defensivo: não deveria acontecer, dado as permissões do canal

        await self._tickets.registrar_mensagem(sessao.id, "helper", message.content, None)

        if sessao.modo == "ia":
            await self._tickets.pausar_ia(sessao.id, message.author.id)
            try:
                await message.channel.send(
                    f"👋 {message.author.mention} entrou na conversa. A IA foi pausada automaticamente "
                    "(Modo Observador). Use `/ia continuar` para devolver a conversa para a IA, ou "
                    "`/ia cooperar` para receber sugestões da IA por DM."
                )
            except discord.HTTPException:
                pass

    def _eh_membro_de_apoio(self, membro: discord.abc.User) -> bool:
        """Verifica se um membro possui o cargo de Staff ou de Helper configurado."""
        cargos_permitidos = self._config.cargos_de_apoio
        if not cargos_permitidos or not isinstance(membro, discord.Member):
            return False
        ids_dos_cargos = {cargo.id for cargo in membro.roles}
        return bool(ids_dos_cargos & set(cargos_permitidos))

    async def _processar_mensagem_do_usuario(self, message: discord.Message, sessao) -> None:
        """Mensagem da pessoa dona do ticket — o comportamento depende do modo atual da sessão."""
        usuario_id = message.author.id
        conteudo = message.content

        if self._db.esta_na_blacklist(usuario_id):
            log.mensagem_ignorada("usuário na blacklist", str(message.author))
            await self._db.incrementar_estatistica("mensagens_ignoradas")
            return

        valida, motivo = mensagem_valida(conteudo, self._config.tamanho_maximo_mensagem)
        if not valida:
            log.mensagem_ignorada(motivo, str(message.author))
            await self._db.incrementar_estatistica("mensagens_ignoradas")
            return

        emocao = emotion.classificar(conteudo)

        # A detecção de crise tem prioridade sobre tudo: mesmo em Modo
        # Observador/Cooperação, uma pessoa em risco recebe a resposta de
        # segurança imediatamente.
        resultado_crise = crisis.analisar(conteudo)
        if resultado_crise.em_crise:
            await self._tratar_crise(message, sessao, conteudo, emocao)
            return

        if sessao.modo == "observador":
            # Um Helper humano está no controle: a IA apenas registra.
            await self._tickets.registrar_mensagem(sessao.id, "user", conteudo, emocao)
            return

        if self._cooldown.em_cooldown(usuario_id):
            restante = self._cooldown.tempo_restante(usuario_id)
            log.mensagem_ignorada(f"usuário em cooldown ({restante:.1f}s restantes)", str(message.author))
            await self._db.incrementar_estatistica("mensagens_ignoradas")
            return

        self._cooldown.registrar_uso(usuario_id)
        await self._db.registrar_usuario_visto(usuario_id)

        if sessao.modo == "cooperacao":
            await self._sugerir_para_helper(message, sessao, conteudo, emocao)
            return

        await self._responder(message, sessao, conteudo, emocao)

    # ------------------------------------------------------------------
    # Modo Cooperação (IA + Helper humano)
    # ------------------------------------------------------------------

    async def _sugerir_para_helper(self, message: discord.Message, sessao, conteudo: str, emocao: str) -> None:
        """
        No Modo Cooperação, a IA nunca responde diretamente à pessoa:
        gera uma sugestão e a envia apenas por DM ao Helper responsável.
        """
        await self._tickets.registrar_mensagem(sessao.id, "user", conteudo, emocao)

        if not sessao.helper_id:
            return  # sem Helper definido, não há para quem sugerir

        try:
            historico = self._memoria.obter_historico(sessao.id)
            sugestao = await self._ia.gerar_resposta(
                historico, conteudo, usuario_id=sessao.id, system_prompt=PROMPT_COOPERACAO
            )
        except ErroDeIA as exc:
            log.erro("Falha ao gerar sugestão para o Helper (Modo Cooperação)", exc)
            return

        try:
            helper = await self.fetch_user(sessao.helper_id)
            await helper.send(
                f"💡 **Sugestão para a sessão #{sessao.id}** (apenas você vê isto):\n\n{sugestao}"
            )
        except discord.HTTPException as exc:
            log.erro("Falha ao enviar sugestão por DM ao Helper", exc)

    # ------------------------------------------------------------------
    # Detecção de crise
    # ------------------------------------------------------------------

    async def _tratar_crise(self, message: discord.Message, sessao, conteudo: str, emocao: str) -> None:
        """
        Interrompe o fluxo normal de IA e responde com uma mensagem de
        segurança fixa, determinística, sempre que houver indícios
        graves de risco à vida na mensagem da pessoa. Também pausa a IA
        automaticamente (Modo Observador), para que um Helper humano
        precise assumir explicitamente a conversa a partir daqui.
        """
        await self._tickets.registrar_mensagem(sessao.id, "user", conteudo, emocao)
        await self._tickets.marcar_crise(sessao.id)
        if sessao.modo == "ia":
            await self._tickets.pausar_ia(sessao.id, helper_id=None)

        resposta = crisis.RESPOSTA_DE_SEGURANCA
        if self._config.enable_crisis_mode and self._config.staff_role_id:
            resposta += (
                f"\n\n_<@&{self._config.staff_role_id}>, um possível sinal de risco foi "
                "identificado nesta conversa e pode merecer atenção da equipe._"
            )

        try:
            await message.reply(resposta, mention_author=False)
        except discord.HTTPException as exc:
            log.erro("Falha ao enviar a resposta de segurança", exc)

        await self._tickets.registrar_mensagem(sessao.id, "assistant", crisis.RESPOSTA_DE_SEGURANCA, None)

    # ------------------------------------------------------------------
    # Conversa normal (IA)
    # ------------------------------------------------------------------

    async def _responder(self, message: discord.Message, sessao, conteudo: str, emocao: str) -> None:
        """
        Gera e envia a resposta da IA. Mostra "💭 Pensando..." e depois
        edita a mesma mensagem com a resposta final — nunca envia duas
        mensagens para o caso comum (respostas que cabem em um bloco).
        """
        inicio = time.monotonic()

        try:
            mensagem_pensando = await message.channel.send("💭 Pensando...")
        except discord.HTTPException:
            mensagem_pensando = None

        try:
            async with message.channel.typing():
                historico = self._memoria.obter_historico(sessao.id)
                resposta = await self._ia.gerar_resposta(historico, conteudo, usuario_id=sessao.id)
                self._memoria.adicionar_interacao(sessao.id, conteudo, resposta)

                await self._tickets.registrar_mensagem(sessao.id, "user", conteudo, emocao)
                await self._tickets.registrar_mensagem(sessao.id, "assistant", resposta, None)

                blocos = self._dividir_em_blocos(resposta)
                if mensagem_pensando is not None:
                    await mensagem_pensando.edit(content=blocos[0])
                    for pedaco in blocos[1:]:
                        await message.channel.send(pedaco)
                else:
                    for pedaco in blocos:
                        await message.reply(pedaco, mention_author=False)

            tempo_ms = (time.monotonic() - inicio) * 1000
            self._soma_tempo_resposta_ms += tempo_ms
            self._contagem_respostas += 1
            log.usuario_atendido(str(message.author), tempo_ms)

        except ErroDeIA as exc:
            log.erro("Nenhum provedor de IA disponível no momento", exc)
            await self._entregar_ou_avisar(
                message, mensagem_pensando,
                "Desculpa, não estou conseguindo pensar direito agora. Pode tentar de novo em alguns instantes? 💙",
            )
        except discord.HTTPException as exc:
            log.erro("Erro ao enviar mensagem no Discord", exc)
        except Exception as exc:  # noqa: BLE001 - nunca deixar o bot travar
            log.erro("Erro inesperado ao processar mensagem", exc)
            await self._entregar_ou_avisar(
                message, mensagem_pensando,
                "Tive um probleminha aqui do meu lado, mas já estou de volta. Pode repetir, por favor?",
            )

    @staticmethod
    async def _entregar_ou_avisar(message: discord.Message, mensagem_pensando, texto: str) -> None:
        """Edita a mensagem de 'Pensando...' com um aviso de erro, ou responde normalmente se ela não existir."""
        if mensagem_pensando is not None:
            try:
                await mensagem_pensando.edit(content=texto)
                return
            except discord.HTTPException:
                pass
        try:
            await message.reply(texto, mention_author=False)
        except discord.HTTPException:
            pass

    @staticmethod
    def _dividir_em_blocos(texto: str, limite: int = 2000) -> list[str]:
        """Divide uma resposta longa em blocos que respeitam o limite do Discord."""
        if len(texto) <= limite:
            return [texto]

        blocos: list[str] = []
        atual = texto
        while len(atual) > limite:
            corte = atual.rfind("\n", 0, limite)
            if corte == -1:
                corte = limite
            blocos.append(atual[:corte].strip())
            atual = atual[corte:].strip()
        if atual:
            blocos.append(atual)
        return blocos

    def _tempo_medio_resposta_ms(self) -> float:
        if self._contagem_respostas == 0:
            return 0.0
        return self._soma_tempo_resposta_ms / self._contagem_respostas

    # ------------------------------------------------------------------
    # Botões do sistema de tickets (chamados a partir de ui.py)
    # ------------------------------------------------------------------

    async def iniciar_conversa(self, interaction: discord.Interaction) -> None:
        """Cria uma sessão privada (ticket) para quem clicou em 'Iniciar Conversa'."""
        if interaction.guild is None:
            await interaction.response.send_message("Isso só funciona dentro de um servidor.", ephemeral=True)
            return

        pode, motivo = await self._tickets.pode_abrir_nova_sessao(interaction.user.id)
        if not pode:
            texto = motivo
            existente = await self._tickets.sessao_ativa_do_usuario(interaction.user.id)
            if existente is not None:
                canal_existente = interaction.guild.get_channel(existente.channel_id)
                if canal_existente is not None:
                    texto += f" Acesse {canal_existente.mention}."
            await interaction.response.send_message(texto, ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            sessao, canal = await self._tickets.abrir_sessao(interaction.guild, interaction.user)
        except Exception as exc:  # noqa: BLE001
            log.erro("Falha ao criar canal de ticket", exc)
            await interaction.followup.send(
                "Não consegui criar sua conversa agora. Tente novamente em instantes.", ephemeral=True
            )
            return

        sessao_anterior = await self._tickets.ultima_sessao_fechada(interaction.user.id)
        if sessao_anterior is not None and sessao_anterior.summary:
            self._memoria.definir_resumo_inicial(
                sessao.id, f"Na última conversa, a pessoa relatou: {sessao_anterior.summary}"
            )

        try:
            embed_boas_vindas = discord.Embed(
                title="🌙 Conversa privada iniciada",
                description=(
                    f"Oi, {interaction.user.mention}! Este é um espaço só seu. Pode falar à vontade "
                    "sobre o que estiver sentindo — estou aqui para ouvir.\n\n"
                    "Quando quiser encerrar, é só usar o botão abaixo."
                ),
                color=discord.Color.dark_purple(),
            )
            await canal.send(embed=embed_boas_vindas, view=ui.TicketView(self))
            if sessao_anterior is not None and sessao_anterior.summary:
                await canal.send(f"💭 *Retomando de onde paramos: {sessao_anterior.summary[:300]}*")
        except discord.HTTPException as exc:
            log.erro("Falha ao enviar mensagem de boas-vindas do ticket", exc)

        await interaction.followup.send(f"Sua conversa privada foi criada: {canal.mention} 💜", ephemeral=True)

    async def solicitar_confirmacao_encerramento(self, interaction: discord.Interaction) -> None:
        """Pede confirmação antes de encerrar de fato uma sessão."""
        canal_id = interaction.channel_id
        sessao = await self._tickets.sessao_por_canal(canal_id)
        if sessao is None:
            await interaction.response.send_message("Essa conversa já não está mais ativa.", ephemeral=True)
            return

        await interaction.response.send_message(
            "Tem certeza que quer encerrar esta conversa? Vou gerar um resumo e fechar o canal.",
            view=ui.ConfirmarEncerramentoView(self),
            ephemeral=True,
        )

    async def confirmar_encerramento(self, interaction: discord.Interaction) -> None:
        """Encerra definitivamente a sessão (resumo, persistência e exclusão do canal)."""
        canal_id = interaction.channel_id
        sessao = await self._tickets.sessao_por_canal(canal_id)
        if sessao is None:
            await interaction.response.edit_message(content="Essa conversa já foi encerrada.", view=None)
            return

        await interaction.response.edit_message(
            content="Encerrando e salvando um resumo da nossa conversa... 💜", view=None
        )

        canal = interaction.channel or self.get_channel(sessao.channel_id)
        if canal is not None:
            await self._tickets.encerrar_definitivamente(sessao, canal, self._ia, self._config, client=self)

    async def continuar_apos_aviso(self, interaction: discord.Interaction) -> None:
        """Cancela um fechamento pendente após o aviso de inatividade."""
        canal_id = interaction.channel_id
        sessao = await self._tickets.sessao_por_canal(canal_id)
        if sessao is None:
            await interaction.response.edit_message(content="Essa conversa já foi encerrada.", view=None)
            return

        await self._tickets.reabrir_sessao(sessao.id)
        await interaction.response.edit_message(content="Ótimo, vamos continuar. Estou por aqui. 💜", view=None)

    # ------------------------------------------------------------------
    # Comandos slash
    # ------------------------------------------------------------------

    def _registrar_comandos(self) -> None:
        """Registra os comandos slash: /health, /version, /painel e /ia."""

        @self.tree.command(name="health", description="Mostra o status atual do bot e dos serviços conectados.")
        async def health(interaction: discord.Interaction) -> None:
            await self._comando_health(interaction)

        @self.tree.command(name="version", description="Mostra a versão atual do bot.")
        async def version_cmd(interaction: discord.Interaction) -> None:
            await self._comando_version(interaction)

        grupo_painel = app_commands.Group(
            name="painel", description="Publica os painéis do bot (apenas administradores)."
        )

        @grupo_painel.command(name="enviar", description="Publica (ou republica) o painel 'Iniciar Conversa'.")
        async def painel_enviar(interaction: discord.Interaction) -> None:
            if not self._eh_administrador(interaction):
                await interaction.response.send_message("Apenas administradores podem usar este comando.", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)
            sucesso = await self._garantir_painel(forcar=True)
            if sucesso:
                await interaction.followup.send("Painel publicado! 💜", ephemeral=True)
            else:
                await interaction.followup.send(
                    "Não consegui publicar o painel — verifique `CANAL_DESABAFOS` e as permissões do bot no canal.",
                    ephemeral=True,
                )

        @grupo_painel.command(name="admin", description="Abre o painel administrativo (configurações da LORA).")
        async def painel_admin(interaction: discord.Interaction) -> None:
            if not self._eh_administrador(interaction):
                await interaction.response.send_message("Apenas administradores podem usar este comando.", ephemeral=True)
                return
            await interaction.response.send_message(embed=montar_embed_admin(), view=PainelAdminView(self), ephemeral=True)

        self.tree.add_command(grupo_painel)

        grupo_ia = app_commands.Group(
            name="ia", description="Controla a IA dentro de uma conversa privada (Staff/Helper)."
        )

        @grupo_ia.command(name="pausar", description="Pausa a IA nesta conversa (Modo Observador) e assume o atendimento.")
        async def ia_pausar(interaction: discord.Interaction) -> None:
            await self._comando_ia_pausar(interaction)

        @grupo_ia.command(name="continuar", description="Devolve a conversa para a IA (encerra o Modo Observador/Cooperação).")
        async def ia_continuar(interaction: discord.Interaction) -> None:
            await self._comando_ia_continuar(interaction)

        @grupo_ia.command(
            name="cooperar",
            description="Ativa o Modo Cooperação: a IA sugere respostas só para você, por DM, sem falar com a pessoa.",
        )
        async def ia_cooperar(interaction: discord.Interaction) -> None:
            await self._comando_ia_cooperar(interaction)

        self.tree.add_command(grupo_ia)

    @staticmethod
    def _eh_administrador(interaction: discord.Interaction) -> bool:
        permissoes = getattr(interaction.user, "guild_permissions", None)
        return bool(permissoes and permissoes.administrator)

    async def _verificar_comando_ia(self, interaction: discord.Interaction) -> "Sessao | None":
        """Validações comuns aos subcomandos de /ia: canal de ticket + permissão de Staff/Helper."""
        if interaction.channel_id is None:
            await interaction.response.send_message("Use este comando dentro do canal de um ticket.", ephemeral=True)
            return None

        sessao = await self._tickets.sessao_por_canal(interaction.channel_id)
        if sessao is None:
            await interaction.response.send_message("Este não é um canal de ticket ativo.", ephemeral=True)
            return None

        if not self._eh_administrador(interaction) and not self._eh_membro_de_apoio(interaction.user):
            await interaction.response.send_message("Apenas a equipe de apoio pode usar este comando.", ephemeral=True)
            return None

        return sessao

    async def _comando_ia_pausar(self, interaction: discord.Interaction) -> None:
        sessao = await self._verificar_comando_ia(interaction)
        if sessao is None:
            return
        await self._tickets.pausar_ia(sessao.id, interaction.user.id)
        await interaction.response.send_message(
            "IA pausada — você está no controle da conversa agora (Modo Observador).", ephemeral=True
        )

    async def _comando_ia_continuar(self, interaction: discord.Interaction) -> None:
        sessao = await self._verificar_comando_ia(interaction)
        if sessao is None:
            return
        await self._tickets.retomar_ia(sessao.id)
        await interaction.response.send_message("A IA está de volta ao controle desta conversa.", ephemeral=True)

    async def _comando_ia_cooperar(self, interaction: discord.Interaction) -> None:
        sessao = await self._verificar_comando_ia(interaction)
        if sessao is None:
            return
        await self._tickets.ativar_cooperacao(sessao.id, interaction.user.id)
        await interaction.response.send_message(
            "Modo Cooperação ativado: a IA vai te enviar sugestões por DM, sem responder diretamente à pessoa.",
            ephemeral=True,
        )

    async def _comando_health(self, interaction: discord.Interaction) -> None:
        """Implementação do comando /health."""
        db_ok = await self._db.esta_saudavel()
        status_provedores = self._ia.status_provedores()
        uptime_formatado = _formatar_duracao(int(time.monotonic() - self._inicio_bot))

        abertas = await self._db.contar_sessoes_abertas()
        fechadas = await self._db.contar_sessoes_fechadas()
        estatisticas = await self._db.obter_estatisticas_completas()
        backup_info = await self._db.ultimo_backup_info()
        media_avaliacoes, total_avaliacoes = await self._db.obter_media_avaliacoes()

        embed = discord.Embed(title="🌙 Status do LORA", color=discord.Color.blurple())
        embed.add_field(name="Discord", value=f"🟢 Conectado ({self.latency * 1000:.0f}ms)", inline=True)
        embed.add_field(name="SQLite", value="🟢 Saudável" if db_ok else "🔴 Com problemas", inline=True)
        embed.add_field(name="Fila de IA", value=f"{self._ia.tamanho_fila()} na espera", inline=True)

        if "gemini" in status_provedores:
            embed.add_field(
                name="Gemini",
                value=f"{_emoji_status(status_provedores['gemini'])} {status_provedores['gemini']} "
                f"({self._ia.modelo_gemini_atual()})",
                inline=True,
            )
        if "groq" in status_provedores:
            embed.add_field(
                name="Groq",
                value=f"{_emoji_status(status_provedores['groq'])} {status_provedores['groq']} "
                f"({self._ia.modelo_groq_atual()})",
                inline=True,
            )

        embed.add_field(name="Tickets ativos", value=str(abertas), inline=True)
        embed.add_field(name="Sessões encerradas", value=str(fechadas), inline=True)
        embed.add_field(name="Fallbacks (total)", value=str(estatisticas.get("respostas_fallback", 0)), inline=True)
        embed.add_field(name="Crises detectadas", value=str(estatisticas.get("crises_detectadas", 0)), inline=True)
        embed.add_field(name="Uptime", value=uptime_formatado, inline=True)
        embed.add_field(
            name="Tempo médio de resposta",
            value=f"{self._tempo_medio_resposta_ms():.0f}ms" if self._contagem_respostas else "sem dados ainda",
            inline=True,
        )
        embed.add_field(name="Uso de memória", value=f"{uso_memoria_mb():.1f} MB", inline=True)
        embed.add_field(name="Último backup", value=backup_info or "nenhum ainda", inline=True)
        embed.add_field(name="Watchdog", value="🟢 Ativo", inline=True)
        embed.add_field(
            name="Avaliação média",
            value=f"⭐ {media_avaliacoes:.1f} ({total_avaliacoes} avaliações)" if total_avaliacoes else "sem avaliações ainda",
            inline=True,
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _comando_version(self, interaction: discord.Interaction) -> None:
        """Implementação do comando /version."""
        provedor_ativo = "Gemini" if "gemini" in self._ia.status_provedores() else "Groq"
        try:
            pasta = os.path.dirname(__file__)
            quantidade_modulos = len([f for f in os.listdir(pasta) if f.endswith(".py")])
        except OSError:
            quantidade_modulos = 0

        embed = discord.Embed(title="🌙 Versão do LORA", color=discord.Color.green())
        embed.add_field(name="Versão", value=version.VERSAO, inline=True)
        embed.add_field(name="Build", value=version.DATA_BUILD, inline=True)
        embed.add_field(name="Arquitetura", value="Sessões privadas + Helper humano (híbrido IA/humano)", inline=True)
        embed.add_field(name="Módulos", value=str(quantidade_modulos), inline=True)
        embed.add_field(name="Modelo Gemini", value=self._ia.modelo_gemini_atual(), inline=True)
        embed.add_field(name="Modelo Groq", value=self._ia.modelo_groq_atual(), inline=True)
        embed.add_field(name="Provedor prioritário", value=provedor_ativo, inline=True)
        embed.add_field(name="Banco de dados", value=f"SQLite ({self._config.db_path})", inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)


def _formatar_duracao(segundos: int) -> str:
    """Formata uma duração em segundos como texto legível (ex: '2h 15m')."""
    horas, resto = divmod(segundos, 3600)
    minutos, segs = divmod(resto, 60)
    if horas:
        return f"{horas}h {minutos}m"
    if minutos:
        return f"{minutos}m {segs}s"
    return f"{segs}s"


def _emoji_status(status: str) -> str:
    """Traduz o status textual de um provedor (ai.py) em um emoji para os embeds."""
    if status == "disponível":
        return "🟢"
    if status == "reativando":
        return "🟡"
    return "🔴"
