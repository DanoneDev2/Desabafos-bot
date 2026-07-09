"""
ticket_manager.py

Gerencia sessões privadas (tickets): criação, localização de sessão
existente, impedimento de múltiplas sessões simultâneas por usuário,
fechamento e reabertura. Toda a regra de negócio de sessão fica
isolada aqui; a persistência em si é delegada a `database.py`, e a
criação de canais é delegada ao discord.py.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord

import emotion
import logger as log
import summarizer
from config import Config
from database import Database

if TYPE_CHECKING:
    from ai import ProvedorDeIA
    from memory import GerenciadorDeMemoria


@dataclass
class Sessao:
    """Representação em memória de uma sessão (ticket) privada."""

    id: int
    user_id: int
    channel_id: int
    status: str
    message_count: int = 0
    summary: str | None = None
    emotion: str | None = None
    crise_detectada: bool = False
    last_activity: str | None = None
    aviso_inatividade_enviado: bool = False
    opened_at: str | None = None

    @classmethod
    def de_linha(cls, linha: dict) -> "Sessao":
        """Constrói uma Sessao a partir de uma linha (dict) vinda do banco."""
        return cls(
            id=linha["id"],
            user_id=linha["user_id"],
            channel_id=linha["channel_id"],
            status=linha["status"],
            message_count=linha.get("message_count", 0) or 0,
            summary=linha.get("summary"),
            emotion=linha.get("emotion"),
            crise_detectada=bool(linha.get("crise_detectada", 0)),
            last_activity=linha.get("last_activity"),
            aviso_inatividade_enviado=bool(linha.get("aviso_inatividade_enviado", 0)),
            opened_at=linha.get("opened_at"),
        )


class GerenciadorDeTickets:
    """Orquestra a criação, localização, fechamento e reabertura de sessões privadas."""

    def __init__(self, db: Database, config: Config, memoria: "GerenciadorDeMemoria | None" = None) -> None:
        self._db = db
        self._config = config
        self._memoria = memoria

    # ------------------------------------------------------------------
    # Localização de sessões
    # ------------------------------------------------------------------

    async def sessao_ativa_do_usuario(self, user_id: int) -> Sessao | None:
        """Retorna a sessão aberta de um usuário, se existir."""
        linha = await self._db.obter_sessao_ativa_por_usuario(user_id)
        return Sessao.de_linha(linha) if linha else None

    async def sessao_por_canal(self, channel_id: int) -> Sessao | None:
        """Retorna a sessão aberta associada a um canal/thread, se existir."""
        linha = await self._db.obter_sessao_por_canal(channel_id)
        if linha and linha["status"] == "aberta":
            return Sessao.de_linha(linha)
        return None

    async def ultima_sessao_fechada(self, user_id: int) -> Sessao | None:
        """Retorna a sessão fechada mais recente de um usuário, para dar continuidade a uma nova conversa."""
        linha = await self._db.obter_ultima_sessao_fechada(user_id)
        return Sessao.de_linha(linha) if linha else None

    # ------------------------------------------------------------------
    # Criação (impede múltiplas sessões por usuário)
    # ------------------------------------------------------------------

    async def pode_abrir_nova_sessao(self, user_id: int) -> tuple[bool, str]:
        """
        Verifica se um usuário pode abrir uma nova sessão: nega se já
        existir uma ativa, ou se o limite global (SESSION_LIMIT) foi
        atingido.
        """
        existente = await self.sessao_ativa_do_usuario(user_id)
        if existente is not None:
            return False, "Você já possui uma conversa ativa."

        if self._config.session_limit > 0:
            abertas = await self._db.contar_sessoes_abertas()
            if abertas >= self._config.session_limit:
                return False, "No momento não há vagas disponíveis para novas conversas. Tente novamente em instantes."

        return True, ""

    async def criar_canal_de_ticket(
        self, guild: discord.Guild, membro: discord.abc.User
    ) -> discord.abc.GuildChannel:
        """Cria o canal (ou thread privada) do ticket, com as permissões corretas."""
        nome = _nome_de_canal(getattr(membro, "display_name", str(membro)))

        if self._config.enable_private_threads:
            canal_ancora = guild.get_channel(self._config.canal_painel)
            if canal_ancora is None:
                raise RuntimeError(
                    "Canal do painel (CANAL_DESABAFOS) não encontrado para criar a thread privada."
                )
            thread = await canal_ancora.create_thread(
                name=nome, type=discord.ChannelType.private_thread, invitable=False
            )
            await thread.add_user(membro)
            return thread

        categoria = guild.get_channel(self._config.category_tickets)
        overwrites: dict = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            membro: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        }
        if guild.me is not None:
            overwrites[guild.me] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, manage_channels=True
            )
        if self._config.staff_role_id:
            staff_role = guild.get_role(self._config.staff_role_id)
            if staff_role is not None:
                overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

        canal = await guild.create_text_channel(
            name=nome, category=categoria, overwrites=overwrites, reason=f"Ticket de {membro}"
        )
        return canal

    async def abrir_sessao(
        self, guild: discord.Guild, membro: discord.abc.User
    ) -> tuple[Sessao, discord.abc.GuildChannel]:
        """Cria o canal do ticket e a sessão correspondente no banco."""
        canal = await self.criar_canal_de_ticket(guild, membro)
        session_id = await self._db.criar_sessao(user_id=membro.id, channel_id=canal.id)
        log.ticket_criado(session_id, str(membro), canal.name)
        sessao = Sessao(id=session_id, user_id=membro.id, channel_id=canal.id, status="aberta")
        return sessao, canal

    # ------------------------------------------------------------------
    # Mensagens da sessão
    # ------------------------------------------------------------------

    async def registrar_mensagem(
        self, session_id: int, role: str, content: str, emotion: str | None = None
    ) -> None:
        """Salva uma mensagem da sessão (histórico persistente, além da memória em RAM)."""
        await self._db.registrar_mensagem_sessao(session_id, role, content, emotion)

    async def obter_historico_bruto(self, session_id: int) -> list[dict]:
        """Retorna todas as mensagens brutas de uma sessão (usado para gerar o resumo final)."""
        return await self._db.obter_mensagens_sessao(session_id)

    # ------------------------------------------------------------------
    # Fechamento e reabertura
    # ------------------------------------------------------------------

    async def fechar_sessao(self, session_id: int, resumo: dict, emotion: str = "") -> None:
        """Marca a sessão como fechada e persiste o resumo gerado pela IA."""
        await self._db.fechar_sessao(
            session_id,
            summary=resumo.get("resumo", ""),
            main_topics=resumo.get("principais_assuntos", ""),
            concerns=resumo.get("preocupacoes", ""),
            goals=resumo.get("objetivos", ""),
            emotion=emotion,
        )
        log.ticket_fechado(session_id)

    async def reabrir_sessao(self, session_id: int) -> None:
        """Cancela um fechamento pendente, mantendo a sessão aberta."""
        await self._db.reabrir_sessao(session_id)
        log.sessao_reaberta(session_id)

    async def encerrar_definitivamente(
        self,
        sessao: Sessao,
        canal: discord.abc.GuildChannel,
        ia: "ProvedorDeIA",
        config: Config,
    ) -> None:
        """
        Fluxo completo e único de encerramento de um ticket, usado tanto
        pelo botão "Encerrar Conversa" quanto pelo fechamento automático
        por inatividade: gera o resumo estruturado via IA, classifica a
        emoção predominante da sessão, persiste o fechamento, limpa a
        memória em RAM e apaga o canal privado.
        """
        mensagens = await self.obter_historico_bruto(sessao.id)
        resumo = await summarizer.gerar_resumo(ia, mensagens, modelo=config.modelo_resumo)
        emocao_predominante = emotion.predominante(
            [m.get("emotion") for m in mensagens if m.get("role") == "user"]
        )

        await self.fechar_sessao(sessao.id, resumo.como_dict(), emotion=emocao_predominante)
        log.resumo_criado(sessao.id, config.modelo_resumo)

        if self._memoria is not None:
            self._memoria.limpar_historico(sessao.id)

        try:
            mensagem_final = "🔒 Conversa encerrada por aqui. Obrigado por compartilhar comigo — cuide-se. 💜"
            if resumo.resumo:
                mensagem_final += f"\n\n*Resumo salvo para dar continuidade, se você voltar: {resumo.resumo[:300]}*"
            await canal.send(mensagem_final)
        except discord.HTTPException as exc:
            log.erro(f"Falha ao enviar mensagem final da sessão #{sessao.id}", exc)

        await asyncio.sleep(5)

        try:
            await canal.delete(reason=f"Ticket #{sessao.id} encerrado")
        except discord.HTTPException as exc:
            log.erro(f"Falha ao apagar o canal da sessão #{sessao.id}", exc)

    async def marcar_aviso_inatividade(self, session_id: int) -> None:
        """Marca que o aviso de inatividade já foi enviado, para não repeti-lo."""
        await self._db.marcar_aviso_inatividade_enviado(session_id)

    async def marcar_crise(self, session_id: int) -> None:
        """Registra que esta sessão teve um episódio de crise detectado."""
        await self._db.marcar_crise(session_id)
        log.crise_detectada(session_id)

    async def sessoes_para_avaliar_fechamento(self) -> list[Sessao]:
        """Retorna todas as sessões abertas, para a rotina de fechamento automático avaliar inatividade."""
        linhas = await self._db.listar_sessoes_abertas_para_verificacao()
        return [Sessao.de_linha(linha) for linha in linhas]


def _nome_de_canal(nome_usuario: str) -> str:
    """Sanitiza um nome de usuário para um nome de canal/thread Discord válido."""
    base = nome_usuario.lower().strip()
    base = re.sub(r"[^a-z0-9\-]+", "-", base)
    base = re.sub(r"-+", "-", base).strip("-")
    if not base:
        base = "usuario"
    return f"🌙・{base}"[:100]
