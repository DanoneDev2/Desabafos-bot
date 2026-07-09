"""
crisis.py

Detector de crise, isolado do restante da lógica de conversa. Sempre
que houver indícios graves (risco de suicídio, automutilação ou crise
aguda), a resposta normal da IA deve ser interrompida e substituída por
uma resposta de segurança fixa, com contato para CVV e emergência.

Este módulo é propositalmente simples e determinístico (baseado em
palavras-chave), em vez de depender de outra chamada de IA: isso o
torna instantâneo, gratuito, e não sujeito a falhas de rede ou de
provedor — características essenciais para um mecanismo de segurança.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Termos fortemente associados a risco iminente de vida. Mantido em
# nível de padrão (não uma lista exaustiva anotada), suficiente para
# acionar uma resposta de segurança sem funcionar como guia de evasão.
_PADROES_RISCO_ALTO = (
    r"\bquero morrer\b",
    r"\bnao quero mais viver\b",
    r"\bvou me matar\b",
    r"\bpensando em suicidio\b",
    r"\bpensei em me matar\b",
    r"\btirar minha vida\b",
    r"\bacabar com (a )?minha vida\b",
    r"\bme cortar\b",
    r"\bme machucar\b",
    r"\bnao aguento mais viver\b",
    r"\bsem motivo para viver\b",
    r"\bplano para (me matar|morrer)\b",
)

_REGEX_RISCO_ALTO = re.compile("|".join(_PADROES_RISCO_ALTO), re.IGNORECASE)


@dataclass(frozen=True)
class ResultadoDeCrise:
    """Resultado da análise de uma mensagem quanto a indícios de crise."""

    em_crise: bool
    motivo: str = ""


def _normalizar(texto: str) -> str:
    """Remove acentos e caixa alta para tornar a detecção mais robusta."""
    sem_acento = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    return sem_acento.lower()


def analisar(texto: str) -> ResultadoDeCrise:
    """
    Analisa uma mensagem em busca de indícios graves de risco à vida.

    Args:
        texto: conteúdo da mensagem do usuário.

    Returns:
        ResultadoDeCrise indicando se a mensagem aciona o modo de crise.
    """
    normalizado = _normalizar(texto)
    encontrado = _REGEX_RISCO_ALTO.search(normalizado)
    if encontrado:
        return ResultadoDeCrise(em_crise=True, motivo="padrão de risco identificado")
    return ResultadoDeCrise(em_crise=False)


RESPOSTA_DE_SEGURANCA = (
    "Sinto muito que você esteja passando por um momento tão difícil. O que você "
    "está sentindo é sério, e eu quero que você tenha ajuda de verdade agora — "
    "algo que eu, como IA, não posso substituir.\n\n"
    "💙 **CVV — Centro de Valorização da Vida**: ligue **188** (gratuito, 24h) "
    "ou acesse **www.cvv.org.br** para conversar por chat.\n"
    "🚨 Em caso de risco imediato: **192** (SAMU) ou **190** (Polícia).\n\n"
    "Você não precisa passar por isso sozinho(a). Enquanto isso, estou aqui — "
    "pode continuar me contando o que está acontecendo."
)
