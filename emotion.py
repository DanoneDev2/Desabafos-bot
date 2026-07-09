"""
emotion.py

Classificador leve do tom emocional de uma mensagem do usuário.
Implementado por heurística de palavras-chave (sem chamada extra a
nenhuma API de IA), para manter o custo, a latência e o consumo de
cota gratuita baixos — coerente com os objetivos de otimização do
projeto.

Categorias: Feliz, Neutro, Triste, Ansioso, Raiva, Medo, Desesperança.
"""

from __future__ import annotations

import unicodedata

CATEGORIAS = ("Feliz", "Neutro", "Triste", "Ansioso", "Raiva", "Medo", "Desesperança")

_PALAVRAS_POR_CATEGORIA: dict[str, tuple[str, ...]] = {
    "Feliz": ("feliz", "alegre", "animado", "grato", "gratidao", "content", "empolgad", "orgulhos"),
    "Triste": ("triste", "chorando", "choro", "sozinho", "sozinha", "vazio", "vazia", "saudade", "deprimid"),
    "Ansioso": ("ansios", "nervos", "aflit", "tenso", "tensa", "preocupad", "sem ar", "coracao acelerado"),
    "Raiva": ("raiva", "puto", "puta", "irritad", "odeio", "revoltad", "furios"),
    "Medo": ("medo", "assustad", "aterroriz", "panico", "receio"),
    "Desesperança": ("sem esperanca", "nao tem solucao", "nao vai melhorar", "de nada adianta", "desisti", "sem saida"),
}


def _normalizar(texto: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    return sem_acento.lower()


def classificar(texto: str) -> str:
    """
    Classifica o tom emocional predominante de uma mensagem.

    Args:
        texto: conteúdo da mensagem do usuário.

    Returns:
        Uma das categorias em CATEGORIAS. Retorna "Neutro" quando
        nenhuma palavra-chave é encontrada.
    """
    normalizado = _normalizar(texto)

    pontuacao: dict[str, int] = {categoria: 0 for categoria in _PALAVRAS_POR_CATEGORIA}
    for categoria, palavras in _PALAVRAS_POR_CATEGORIA.items():
        for palavra in palavras:
            if palavra in normalizado:
                pontuacao[categoria] += 1

    melhor_categoria = max(pontuacao, key=pontuacao.get)
    if pontuacao[melhor_categoria] == 0:
        return "Neutro"
    return melhor_categoria


def predominante(emocoes: list[str]) -> str:
    """
    Retorna a emoção mais frequente em uma lista (ex: todas as emoções
    classificadas ao longo de uma sessão). Retorna string vazia se a
    lista estiver vazia, para não sobrescrever um valor já salvo.
    """
    validas = [e for e in emocoes if e]
    if not validas:
        return ""
    contagem: dict[str, int] = {}
    for emocao in validas:
        contagem[emocao] = contagem.get(emocao, 0) + 1
    return max(contagem, key=contagem.get)
