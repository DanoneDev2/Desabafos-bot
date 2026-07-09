"""
utils.py

Funções utilitárias pequenas e reutilizáveis: controle de cooldown por
usuário e validações de mensagens recebidas.
"""

from __future__ import annotations

import random
import time

# Códigos HTTP considerados temporários/retentáveis.
CODIGOS_HTTP_RETENTAVEIS = {408, 429, 500, 502, 503, 504}


class ControladorDeCooldown:
    """Implementa um cooldown simples por usuário, em memória."""

    def __init__(self, segundos: int) -> None:
        self._segundos = segundos
        self._ultimo_uso: dict[int, float] = {}

    def em_cooldown(self, usuario_id: int) -> bool:
        """Retorna True se o usuário ainda estiver em cooldown."""
        ultimo = self._ultimo_uso.get(usuario_id)
        if ultimo is None:
            return False
        return (time.monotonic() - ultimo) < self._segundos

    def tempo_restante(self, usuario_id: int) -> float:
        """Retorna quantos segundos faltam para o cooldown acabar."""
        ultimo = self._ultimo_uso.get(usuario_id)
        if ultimo is None:
            return 0.0
        restante = self._segundos - (time.monotonic() - ultimo)
        return max(0.0, restante)

    def registrar_uso(self, usuario_id: int) -> None:
        """Marca o instante atual como o último uso do usuário."""
        self._ultimo_uso[usuario_id] = time.monotonic()


def mensagem_valida(conteudo: str, tamanho_maximo: int) -> tuple[bool, str]:
    """
    Valida se uma mensagem pode ser processada pela IA.

    Args:
        conteudo: texto da mensagem recebida.
        tamanho_maximo: limite de caracteres aceito.

    Returns:
        Tupla (valida, motivo). Se válida, motivo é uma string vazia.
    """
    texto = conteudo.strip()

    if not texto:
        return False, "mensagem vazia"

    if len(texto) > tamanho_maximo:
        return False, f"mensagem excede o limite de {tamanho_maximo} caracteres"

    return True, ""


def calcular_backoff(tentativa: int, base_segundos: float, teto_segundos: float) -> float:
    """
    Calcula o tempo de espera para a próxima tentativa usando backoff
    exponencial com jitter (para evitar que múltiplas retentativas
    caiam no mesmo instante).

    Args:
        tentativa: número da tentativa atual, começando em 1.
        base_segundos: tempo base de espera.
        teto_segundos: tempo máximo de espera permitido.

    Returns:
        Tempo de espera em segundos.
    """
    espera_bruta = base_segundos * (2 ** (tentativa - 1))
    espera_limitada = min(espera_bruta, teto_segundos)
    jitter = random.uniform(0, espera_limitada * 0.25)
    return espera_limitada + jitter


def extrair_codigo_http(exc: Exception) -> int | None:
    """
    Tenta extrair um código de status HTTP de uma exceção de biblioteca
    de IA (Gemini, Groq, ou genérica), sem depender de tipos específicos
    de cada SDK.
    """
    for atributo in ("status_code", "code", "http_status"):
        valor = getattr(exc, atributo, None)
        if isinstance(valor, int):
            return valor

    resposta = getattr(exc, "response", None)
    if resposta is not None:
        codigo = getattr(resposta, "status_code", None)
        if isinstance(codigo, int):
            return codigo

    return None


def eh_erro_retentavel(exc: Exception) -> bool:
    """
    Decide se um erro de provedor de IA justifica uma nova tentativa:
    timeouts e códigos HTTP temporários (429, 500, 502, 503, 504).
    """
    nome_tipo = type(exc).__name__.lower()
    if "timeout" in nome_tipo:
        return True

    codigo = extrair_codigo_http(exc)
    if codigo is not None and codigo in CODIGOS_HTTP_RETENTAVEIS:
        return True

    mensagem = str(exc).lower()
    return any(str(codigo) in mensagem for codigo in CODIGOS_HTTP_RETENTAVEIS) or "timeout" in mensagem


def eh_erro_contexto_muito_grande(exc: Exception) -> bool:
    """Detecta heuristicamente erros de contexto/prompt muito longo."""
    mensagem = str(exc).lower()
    termos = ("context length", "too many tokens", "context_length", "token limit", "maximum context")
    return any(termo in mensagem for termo in termos)


def uso_memoria_mb() -> float:
    """
    Retorna o uso aproximado de memória RAM do processo atual, em MB.
    Usa apenas a biblioteca padrão (sem dependências extras como psutil),
    o que mantém o bot leve o suficiente para rodar em dispositivos
    modestos e no Termux.
    """
    try:
        import resource

        uso_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # No Linux, ru_maxrss vem em KB; no macOS, vem em bytes.
        import sys

        if sys.platform == "darwin":
            return uso_kb / (1024 * 1024)
        return uso_kb / 1024
    except ImportError:
        # "resource" não existe no Windows; sem dependência extra, não há
        # como medir com precisão — retorna 0.0 nesse caso.
        return 0.0
