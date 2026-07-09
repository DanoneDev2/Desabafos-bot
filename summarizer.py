"""
summarizer.py

Gera o resumo automático de uma sessão encerrada: resumo geral,
principais assuntos, preocupações e objetivos mencionados. Reutiliza o
`ProvedorDeIA` já existente (com toda a fila, retry e circuit breaker
de `ai.py`), em vez de duplicar essa lógica — apenas monta um prompt
especializado de resumo.

Emoções detectadas não são geradas aqui: são classificadas mensagem a
mensagem por `emotion.py` (heurística, sem custo de API) e agregadas
pelo chamador (`events.py`) ao fechar a sessão.
"""

from __future__ import annotations

from dataclasses import dataclass

import logger as log
from ai import ErroDeIA, ProvedorDeIA

_PROMPT_RESUMO = (
    "Você vai analisar uma conversa de apoio emocional já encerrada e produzir "
    "um resumo estruturado, para servir de contexto em uma futura conversa com "
    "a mesma pessoa. Responda ESTRITAMENTE no formato abaixo, em português, de "
    "forma objetiva (1 a 3 frases por campo). Não escreva nada fora deste formato.\n\n"
    "RESUMO: <resumo geral da conversa>\n"
    "ASSUNTOS: <principais assuntos, separados por vírgula>\n"
    "PREOCUPACOES: <principais preocupações mencionadas, separadas por vírgula>\n"
    "OBJETIVOS: <objetivos ou desejos mencionados pela pessoa, separados por vírgula>"
)

_CAMPOS = {
    "RESUMO": "resumo",
    "ASSUNTOS": "principais_assuntos",
    "PREOCUPACOES": "preocupacoes",
    "OBJETIVOS": "objetivos",
}


@dataclass
class ResumoDeSessao:
    """Resumo estruturado de uma sessão encerrada."""

    resumo: str = ""
    principais_assuntos: str = ""
    preocupacoes: str = ""
    objetivos: str = ""

    def como_dict(self) -> dict[str, str]:
        return {
            "resumo": self.resumo,
            "principais_assuntos": self.principais_assuntos,
            "preocupacoes": self.preocupacoes,
            "objetivos": self.objetivos,
        }


def _parsear_resposta(texto: str) -> ResumoDeSessao:
    """Extrai os campos estruturados da resposta da IA, ignorando linhas fora do formato."""
    resultado = ResumoDeSessao()
    for linha in texto.splitlines():
        if ":" not in linha:
            continue
        chave, _, valor = linha.partition(":")
        atributo = _CAMPOS.get(chave.strip().upper())
        if atributo:
            setattr(resultado, atributo, valor.strip())
    return resultado


def _resumo_heuristico(mensagens: list[dict]) -> ResumoDeSessao:
    """Fallback simples (sem IA), usado apenas se todos os provedores falharem."""
    textos_usuario = [m["content"] for m in mensagens if m.get("role") == "user"]
    amostra = " ".join(textos_usuario)[:500]
    return ResumoDeSessao(resumo=amostra or "Conversa encerrada sem conteúdo suficiente para resumo.")


async def gerar_resumo(ia: ProvedorDeIA, mensagens: list[dict], modelo: str = "") -> ResumoDeSessao:
    """
    Gera o resumo estruturado de uma sessão encerrada.

    Nunca lança exceção: se a IA falhar, cai em um resumo heurístico
    simples, pois o fechamento da sessão não pode travar por causa
    disso.

    Args:
        ia: instância compartilhada de ProvedorDeIA (mesma fila/retry/circuit breaker).
        mensagens: mensagens brutas da sessão, em ordem cronológica.
        modelo: nome do modelo usado (apenas para fins de log).
    """
    if not mensagens:
        return ResumoDeSessao(resumo="Conversa encerrada sem mensagens.")

    historico = [{"role": m["role"], "content": m["content"]} for m in mensagens[:-1]]
    ultima_mensagem = mensagens[-1]["content"]
    pedido_de_resumo = f"{_PROMPT_RESUMO}\n\nÚltima mensagem da pessoa: {ultima_mensagem}"

    try:
        resposta = await ia.gerar_resposta(historico, pedido_de_resumo, modelo=modelo or None)
        return _parsear_resposta(resposta)
    except ErroDeIA as exc:
        log.erro("Falha ao gerar resumo via IA, usando resumo heurístico", exc)
        return _resumo_heuristico(mensagens)
