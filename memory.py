"""
memory.py

Gerencia o histórico de conversas de cada usuário de forma isolada.
Implementação em RAM (dicionário protegido), pronta para ser trocada
futuramente por um backend persistente sem alterar a interface pública
desta classe.

ATUALIZAÇÃO — Gerenciamento inteligente de contexto:
    - quando o histórico de um usuário cresce demais, as mensagens mais
      antigas são condensadas em um resumo automático (sem chamadas
      extras à IA, para não consumir cota das APIs gratuitas);
    - o contexto efetivamente enviado à IA pode ser menor do que o
      total armazenado, via `contexto_maximo_envio`;
    - em caso de erro de "contexto muito grande" retornado por um
      provedor, `reduzir_contexto_temporario` permite encolher o
      histórico enviado e tentar novamente na mesma rodada.

A API pública original (obter_historico, adicionar_interacao,
limpar_historico, total_usuarios_ativos) permanece 100% compatível.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class Mensagem:
    """Representa uma única mensagem no histórico de um usuário."""

    role: str  # "user" ou "assistant"
    content: str


@dataclass
class _EstadoUsuario:
    """Estado interno mantido por usuário (não exposto publicamente)."""

    mensagens: deque[Mensagem]
    resumo: str = ""
    ultima_atividade: float = field(default_factory=time.monotonic)
    reducao_temporaria_ativa: bool = False


class GerenciadorDeMemoria:
    """
    Mantém o histórico de conversas de cada usuário separadamente.

    O histórico de um usuário NUNCA é misturado com o de outro. Cada
    usuário possui sua própria fila (deque) com tamanho máximo
    configurável. Quando mensagens antigas saem da fila, seu conteúdo é
    condensado em um resumo textual simples, preservado no início do
    contexto enviado à IA.
    """

    def __init__(
        self,
        max_history: int,
        contexto_maximo_envio: int | None = None,
        resumo_max_caracteres: int = 800,
    ) -> None:
        if max_history < 1:
            raise ValueError("max_history deve ser maior ou igual a 1")

        self._max_history = max_history
        self._contexto_maximo_envio = contexto_maximo_envio or max_history
        self._resumo_max_caracteres = resumo_max_caracteres
        self._capacidade_mensagens = max_history * 2  # cada rodada = 1 user + 1 assistant

        self._estados: dict[int, _EstadoUsuario] = {}

    # ------------------------------------------------------------------
    # API pública (compatível com a versão anterior)
    # ------------------------------------------------------------------

    def obter_historico(self, usuario_id: int) -> list[dict[str, str]]:
        """
        Retorna o histórico do usuário no formato usado pelos provedores
        de IA. Se houver um resumo de mensagens antigas, ele é incluído
        como um par sintético user/assistant no início do histórico,
        preservando a alternância de papéis exigida pelos provedores.
        """
        estado = self._estados.get(usuario_id)
        if not estado:
            return []

        historico: list[dict[str, str]] = []

        if estado.resumo:
            historico.append(
                {
                    "role": "user",
                    "content": f"[Resumo do início desta conversa, para contexto: {estado.resumo}]",
                }
            )
            historico.append(
                {
                    "role": "assistant",
                    "content": "Entendido, vou levar esse contexto em conta.",
                }
            )

        limite_mensagens = self._contexto_maximo_envio * 2
        mensagens_recentes = list(estado.mensagens)
        if estado.reducao_temporaria_ativa:
            # Em modo de redução temporária, envia só a metade mais recente.
            mensagens_recentes = mensagens_recentes[-max(2, limite_mensagens // 2):]
        else:
            mensagens_recentes = mensagens_recentes[-limite_mensagens:]

        historico.extend({"role": msg.role, "content": msg.content} for msg in mensagens_recentes)
        return historico

    def adicionar_interacao(self, usuario_id: int, mensagem_usuario: str, resposta_ia: str) -> None:
        """Adiciona uma rodada completa (pergunta + resposta) ao histórico do usuário."""
        estado = self._estados.setdefault(
            usuario_id,
            _EstadoUsuario(mensagens=deque()),
        )

        estado.mensagens.append(Mensagem(role="user", content=mensagem_usuario))
        estado.mensagens.append(Mensagem(role="assistant", content=resposta_ia))
        estado.ultima_atividade = time.monotonic()
        estado.reducao_temporaria_ativa = False  # nova rodada bem-sucedida encerra o modo reduzido

        self._aplicar_remocao_inteligente(estado)

    def limpar_historico(self, usuario_id: int) -> None:
        """Remove todo o histórico de um usuário específico."""
        self._estados.pop(usuario_id, None)

    def total_usuarios_ativos(self) -> int:
        """Retorna quantos usuários possuem histórico ativo em memória."""
        return len(self._estados)

    # ------------------------------------------------------------------
    # Novas capacidades (gerenciamento inteligente de contexto)
    # ------------------------------------------------------------------

    def reduzir_contexto_temporario(self, usuario_id: int) -> bool:
        """
        Ativa o modo de contexto reduzido para o usuário (usado quando a
        API retorna erro de contexto muito grande). A próxima chamada a
        `obter_historico` enviará apenas metade das mensagens recentes.

        Returns:
            True se havia histórico para reduzir, False se o usuário
            já não tinha nenhum contexto armazenado.
        """
        estado = self._estados.get(usuario_id)
        if not estado or not estado.mensagens:
            return False
        estado.reducao_temporaria_ativa = True
        return True

    def limpar_inativos(self, max_idade_segundos: int) -> int:
        """
        Remove da memória usuários sem atividade recente, para economizar
        RAM. Retorna quantos usuários foram removidos.
        """
        agora = time.monotonic()
        inativos = [
            usuario_id
            for usuario_id, estado in self._estados.items()
            if (agora - estado.ultima_atividade) > max_idade_segundos
        ]
        for usuario_id in inativos:
            del self._estados[usuario_id]
        return len(inativos)

    # ------------------------------------------------------------------
    # Internos
    # ------------------------------------------------------------------

    def _aplicar_remocao_inteligente(self, estado: _EstadoUsuario) -> None:
        """
        Quando o histórico armazenado excede a capacidade configurada,
        remove as mensagens mais antigas e condensa seu conteúdo em um
        resumo textual simples (sem custo de API extra).
        """
        removidas: list[Mensagem] = []
        while len(estado.mensagens) > self._capacidade_mensagens:
            removidas.append(estado.mensagens.popleft())

        if removidas:
            self._atualizar_resumo(estado, removidas)

    def _atualizar_resumo(self, estado: _EstadoUsuario, removidas: list[Mensagem]) -> None:
        """Condensa mensagens removidas em um resumo curto, com tamanho limitado."""
        trecho_novo = " ".join(
            f"{'Usuário' if msg.role == 'user' else 'IA'}: {msg.content.strip()}"
            for msg in removidas
            if msg.content.strip()
        )

        resumo_combinado = f"{estado.resumo} {trecho_novo}".strip() if estado.resumo else trecho_novo

        if len(resumo_combinado) > self._resumo_max_caracteres:
            # Mantém a parte mais recente do resumo, descartando o início.
            resumo_combinado = resumo_combinado[-self._resumo_max_caracteres:]
            corte_espaco = resumo_combinado.find(" ")
            if corte_espaco != -1:
                resumo_combinado = resumo_combinado[corte_espaco + 1:]

        estado.resumo = resumo_combinado
