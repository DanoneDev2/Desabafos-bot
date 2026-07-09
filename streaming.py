"""
streaming.py

Define interfaces desacopladas para respostas de IA, preparando o
projeto para streaming futuro sem exigir que os provedores atuais
(Gemini/Groq, via ai.py) implementem isso hoje.

Nenhum provedor atual faz streaming de fato: `ProvedorDeIA.gerar_resposta`
continua retornando a resposta completa de uma vez. Este módulo apenas
define o contrato que uma implementação futura de streaming deverá
seguir, e um adaptador que permite consumir a interface atual (não
streaming) como se fosse um streaming de um único pedaço — assim, o
código que consome respostas (ex: `events.py`) já pode ser escrito
contra a interface de streaming, e passará a se beneficiar de verdade
quando um provedor com streaming for adicionado, sem precisar mudar.
"""

from __future__ import annotations

from typing import AsyncIterator, Protocol, runtime_checkable


@runtime_checkable
class GeradorDeRespostaStreaming(Protocol):
    """
    Contrato que um provedor de IA com streaming real deverá implementar
    no futuro (ex: Gemini com `stream=True`).
    """

    def gerar_stream(
        self, historico: list[dict[str, str]], nova_mensagem: str
    ) -> AsyncIterator[str]:
        """Deve retornar um iterador assíncrono de pedaços (chunks) de texto."""
        ...


class AdaptadorSemStreaming:
    """
    Adapta um provedor de IA não-streaming (como o `ProvedorDeIA` atual)
    para a interface `GeradorDeRespostaStreaming`, entregando a resposta
    completa como um único chunk.

    Isso permite que o código consumidor (ex: a edição progressiva da
    mensagem "💭 Pensando...") já seja escrito de forma genérica contra
    `gerar_stream`, e comece a exibir texto incrementalmente de verdade
    assim que um provedor com streaming real for conectado — sem
    precisar reescrever `events.py`.
    """

    def __init__(self, provedor) -> None:  # noqa: ANN001 - referência estrutural a ProvedorDeIA
        self._provedor = provedor

    async def gerar_stream(
        self, historico: list[dict[str, str]], nova_mensagem: str
    ) -> AsyncIterator[str]:
        resposta_completa = await self._provedor.gerar_resposta(historico, nova_mensagem)
        yield resposta_completa
