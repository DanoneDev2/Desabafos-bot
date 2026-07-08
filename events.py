"""
events.py

Contém a classe do bot Discord e o tratamento de eventos (on_ready,
on_message, on_disconnect, etc). Toda a lógica de IA e memória é
delegada para ai.py e memory.py — este arquivo apenas orquestra.

ATUALIZAÇÃO:
    - checagem de blacklist (persistida via SQLite) antes de responder;
    - registro de usuários únicos e estatísticas de uso;
    - comandos slash /health e /version;
    - disparo das tarefas em background (backup, limpeza, watchdog).
"""

from __future__ import annotations

import time

import discord
from discord import app_commands

import logger as log
import scheduler
import version
from ai import ErroDeIA, ProvedorDeIA
from config import Config
from database import Database
from memory import GerenciadorDeMemoria
from utils import ControladorDeCooldown, mensagem_valida


class BotDeDesabafos(discord.Client):
    """
    Cliente Discord especializado em atender um único canal de
    desabafos, mantendo memória isolada por usuário e delegando a
    geração de respostas para um ProvedorDeIA.
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
        self._cooldown = ControladorDeCooldown(segundos=config.cooldown_segundos)
        self._ia = ProvedorDeIA(config, memoria=self._memoria, registrar_estatistica=self._registrar_estatistica)
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

    # ------------------------------------------------------------------
    # Ciclo de vida
    # ------------------------------------------------------------------

    async def setup_hook(self) -> None:
        """
        Executado pelo discord.py dentro do event loop, antes de conectar
        totalmente. É o lugar correto para inicializar recursos
        assíncronos como o banco de dados (migração automática incluída).
        """
        await self._db.iniciar()

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

        if not self._tarefas_background:
            self._tarefas_background = scheduler.iniciar_tarefas_em_background(
                client=self, db=self._db, memoria=self._memoria, ia=self._ia, config=self._config
            )

    async def on_disconnect(self) -> None:
        """Disparado quando a conexão com o Discord cai."""
        self._tentativas_reconexao += 1
        log.reconexao(self._tentativas_reconexao)

    async def on_message(self, message: discord.Message) -> None:
        """
        Processa cada mensagem recebida, respondendo apenas quando
        todas as condições do canal de desabafos forem atendidas.
        """
        if not self._deve_processar(message):
            return

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

        if self._cooldown.em_cooldown(usuario_id):
            restante = self._cooldown.tempo_restante(usuario_id)
            log.mensagem_ignorada(f"usuário em cooldown ({restante:.1f}s restantes)", str(message.author))
            await self._db.incrementar_estatistica("mensagens_ignoradas")
            return

        self._cooldown.registrar_uso(usuario_id)
        await self._db.registrar_usuario_visto(usuario_id)

        await self._responder(message, usuario_id, conteudo)

    def _deve_processar(self, message: discord.Message) -> bool:
        """Verifica se a mensagem atende aos critérios para ser respondida."""
        if message.author.bot:
            return False

        if message.channel.id != self._config.canal_desabafos:
            return False

        if not message.content or not message.content.strip():
            return False

        return True

    async def _responder(self, message: discord.Message, usuario_id: int, conteudo: str) -> None:
        """
        Gera e envia a resposta da IA, tratando exceções sem derrubar o
        bot. O indicador "digitando..." é renovado automaticamente pelo
        próprio discord.py enquanto o bloco `async with typing()` estiver
        ativo, então respostas demoradas continuam mostrando o indicador.
        """
        inicio = time.monotonic()

        try:
            async with message.channel.typing():
                historico = self._memoria.obter_historico(usuario_id)
                resposta = await self._ia.gerar_resposta(historico, conteudo, usuario_id=usuario_id)
                self._memoria.adicionar_interacao(usuario_id, conteudo, resposta)

                for pedaco in self._dividir_em_blocos(resposta):
                    await message.reply(pedaco, mention_author=False)

            tempo_ms = (time.monotonic() - inicio) * 1000
            self._soma_tempo_resposta_ms += tempo_ms
            self._contagem_respostas += 1
            log.usuario_atendido(str(message.author), tempo_ms)

        except ErroDeIA as exc:
            log.erro("Nenhum provedor de IA disponível no momento", exc)
            await message.reply(
                "Desculpa, não estou conseguindo pensar direito agora. "
                "Pode tentar de novo em alguns instantes? 💙",
                mention_author=False,
            )
        except discord.HTTPException as exc:
            log.erro("Erro ao enviar mensagem no Discord", exc)
        except Exception as exc:  # noqa: BLE001 - nunca deixar o bot travar
            log.erro("Erro inesperado ao processar mensagem", exc)
            await message.reply(
                "Tive um probleminha aqui do meu lado, mas já estou de volta. "
                "Pode repetir, por favor?",
                mention_author=False,
            )

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
    # Comandos slash
    # ------------------------------------------------------------------

    def _registrar_comandos(self) -> None:
        """Registra os comandos slash /health e /version na árvore de comandos."""

        @self.tree.command(name="health", description="Mostra o status atual do bot e dos serviços conectados.")
        async def health(interaction: discord.Interaction) -> None:
            await self._comando_health(interaction)

        @self.tree.command(name="version", description="Mostra a versão atual do bot.")
        async def version_cmd(interaction: discord.Interaction) -> None:
            await self._comando_version(interaction)

    async def _comando_health(self, interaction: discord.Interaction) -> None:
        """Implementação do comando /health."""
        db_ok = await self._db.esta_saudavel()
        status_provedores = self._ia.status_provedores()
        uptime_segundos = int(time.monotonic() - self._inicio_bot)
        uptime_formatado = _formatar_duracao(uptime_segundos)

        embed = discord.Embed(title="Status do Bot de Desabafos", color=discord.Color.blurple())
        embed.add_field(name="Discord", value=f"🟢 Conectado ({self.latency * 1000:.0f}ms de latência)", inline=False)
        embed.add_field(name="SQLite", value="🟢 Saudável" if db_ok else "🔴 Com problemas", inline=False)

        for provedor, status in status_provedores.items():
            emoji = "🟢" if status == "disponível" else ("🟡" if status == "reativando" else "🔴")
            embed.add_field(name=provedor.capitalize(), value=f"{emoji} {status}", inline=True)

        embed.add_field(name="Uptime", value=uptime_formatado, inline=True)
        embed.add_field(
            name="Tempo médio de resposta",
            value=f"{self._tempo_medio_resposta_ms():.0f}ms" if self._contagem_respostas else "sem dados ainda",
            inline=True,
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _comando_version(self, interaction: discord.Interaction) -> None:
        """Implementação do comando /version."""
        provedor_ativo = "Gemini" if "gemini" in self._ia.status_provedores() else "Groq"

        embed = discord.Embed(title="Versão do Bot de Desabafos", color=discord.Color.green())
        embed.add_field(name="Versão", value=version.VERSAO, inline=True)
        embed.add_field(name="Build", value=version.DATA_BUILD, inline=True)
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
