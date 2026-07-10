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

IMPORTANTE (v4.0): mensagens com role="helper" (registradas quando um
humano assume a conversa no Modo Observador/Cooperação) são sempre
excluídas do histórico enviado à IA — elas não fazem parte da
alternância user/model que os provedores esperam, e o resumo deve
refletir a conversa entre a pessoa e a IA/o Helper, não instruções
internas da equipe.
"""

from __future__ import annotations

from dataclasses import dataclass

import logger as log
from ai import ErroDeIA, ProvedorDeIA
from prompts import PROMPT_RESUMO

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


async def gerar_resumo(
    ia: ProvedorDeIA, mensagens: list[dict], modelo: str = "", prompt_resumo: str = ""
) -> ResumoDeSessao:
    """
    Gera o resumo estruturado de uma sessão encerrada.

    Nunca lança exceção: se a IA falhar, cai em um resumo heurístico
    simples, pois o fechamento da sessão não pode travar por causa
    disso.

    Args:
        ia: instância compartilhada de ProvedorDeIA (mesma fila/retry/circuit breaker).
        mensagens: mensagens brutas da sessão, em ordem cronológica (podem
            incluir mensagens de Helper, que são sempre ignoradas aqui).
        modelo: nome do modelo usado para este resumo (SUMMARY_MODEL).
        prompt_resumo: prompt de resumo customizado (do painel administrativo);
            usa `PROMPT_RESUMO` como padrão se vazio.
    """
    conversa = [m for m in mensagens if m.get("role") in ("user", "assistant")]
    if not conversa:
        return ResumoDeSessao(resumo="Conversa encerrada sem mensagens.")

    historico = [{"role": m["role"], "content": m["content"]} for m in conversa[:-1]]
    ultima_mensagem = conversa[-1]["content"]
    texto_prompt = prompt_resumo or PROMPT_RESUMO
    pedido_de_resumo = f"{texto_prompt}\n\nÚltima mensagem da pessoa: {ultima_mensagem}"

    try:
        resposta = await ia.gerar_resposta(historico, pedido_de_resumo, modelo=modelo or None)
        return _parsear_resposta(resposta)
    except ErroDeIA as exc:
        log.erro("Falha ao gerar resumo via IA, usando resumo heurístico", exc)
        return _resumo_heuristico(mensagens)
