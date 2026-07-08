"""
ai.py

Isola toda a lógica de comunicação com provedores de IA. Nenhuma
lógica relacionada ao Discord deve existir neste módulo.

Prioridade de provedores:
    1. Google Gemini
    2. Groq (fallback automático em caso de falha/indisponibilidade)

ATUALIZAÇÃO — a interface pública (gerar_resposta) permanece a mesma,
mas agora, internamente:
    - todas as chamadas passam por uma fila assíncrona (asyncio.Queue),
      garantindo ordem correta e evitando chamadas simultâneas que
      sobrecarreguem as APIs gratuitas;
    - falhas temporárias (timeout, 429, 500, 502, 503, 504) acionam
      retry com backoff exponencial, com número de tentativas
      configurável;
    - falhas consecutivas de um provedor abrem um circuit breaker,
      marcando-o temporariamente indisponível e usando o secundário,
      com reativação automática após um tempo configurável;
    - erros de "contexto muito grande" acionam redução automática do
      histórico enviado (via memory.py) e uma nova tentativa.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from google import genai
from groq import Groq
from google.genai import types

import logger as log
from config import Config
from memory import GerenciadorDeMemoria
from prompts import SYSTEM_PROMPT
from utils import calcular_backoff, eh_erro_contexto_muito_grande, eh_erro_retentavel


class ErroDeIA(Exception):
    """Erro genérico ao tentar gerar uma resposta de IA."""


@dataclass
class _EstadoCircuitBreaker:
    """Estado de circuito por provedor (fechado = disponível)."""

    falhas_consecutivas: int = 0
    aberto_ate: float | None = None  # timestamp monotonic; None = fechado


class _CircuitBreaker:
    """
    Implementa um circuit breaker simples por provedor: após N falhas
    consecutivas, o provedor fica marcado como indisponível por um
    período configurável, sendo automaticamente reativado depois.
    """

    def __init__(self, falhas_para_abrir: int, timeout_segundos: int) -> None:
        self._falhas_para_abrir = falhas_para_abrir
        self._timeout_segundos = timeout_segundos
        self._estados: dict[str, _EstadoCircuitBreaker] = {}

    def disponivel(self, provedor: str) -> bool:
        estado = self._estados.setdefault(provedor, _EstadoCircuitBreaker())
        if estado.aberto_ate is None:
            return True
        if time.monotonic() >= estado.aberto_ate:
            # Meio-aberto: permite uma tentativa de reativação.
            return True
        return False

    def registrar_sucesso(self, provedor: str) -> None:
        self._estados[provedor] = _EstadoCircuitBreaker()

    def registrar_falha(self, provedor: str) -> None:
        estado = self._estados.setdefault(provedor, _EstadoCircuitBreaker())
        estado.falhas_consecutivas += 1
        if estado.falhas_consecutivas >= self._falhas_para_abrir:
            estado.aberto_ate = time.monotonic() + self._timeout_segundos
            log.aviso(
                f"Circuit breaker aberto para '{provedor}' por {self._timeout_segundos}s "
                f"após {estado.falhas_consecutivas} falhas consecutivas."
            )

    def status(self, provedor: str) -> str:
        estado = self._estados.get(provedor)
        if not estado or estado.aberto_ate is None:
            return "disponível"
        if time.monotonic() >= estado.aberto_ate:
            return "reativando"
        return "indisponível"


@dataclass
class _RequisicaoIA:
    """Item colocado na fila assíncrona de requisições à IA."""

    historico: list[dict[str, str]]
    nova_mensagem: str
    usuario_id: Optional[int]
    future: "asyncio.Future[str]"


class ProvedorDeIA:
    """
    Responsável exclusivamente por gerar respostas de IA a partir de um
    histórico de conversa e uma nova mensagem. Não conhece nada sobre
    Discord, eventos ou canais.
    """

    def __init__(
        self,
        config: Config,
        memoria: GerenciadorDeMemoria | None = None,
        registrar_estatistica: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._config = config
        self._memoria = memoria
        self._registrar_estatistica = registrar_estatistica

        self._gemini_disponivel = bool(config.api_key_gemini)
        self._groq_disponivel = bool(config.api_key_groq)

        if self._gemini_disponivel:
            self._cliente_gemini = genai.Client(
                api_key=config.api_key_gemini
             )
        else:
            self._cliente_gemini = None

        self._cliente_groq = Groq(api_key=config.api_key_groq) if self._groq_disponivel else None

        self._circuit_breaker = _CircuitBreaker(
            falhas_para_abrir=config.circuit_breaker_falhas_consecutivas,
            timeout_segundos=config.circuit_breaker_timeout_segundos,
        )

        # Fila assíncrona: serializa as chamadas à IA para não sobrecarregar
        # as APIs gratuitas nem estourar limites de taxa.
        self._fila: "asyncio.Queue[_RequisicaoIA]" = asyncio.Queue(maxsize=config.fila_ia_tamanho_maximo)
        self._worker_iniciado = False

    # ------------------------------------------------------------------
    # Interface pública (compatível com a versão anterior)
    # ------------------------------------------------------------------

    async def gerar_resposta(
        self,
        historico: list[dict[str, str]],
        nova_mensagem: str,
        usuario_id: int | None = None,
    ) -> str:
        """
        Gera uma resposta de IA. A chamada é colocada em uma fila
        assíncrona para garantir ordem e evitar sobrecarga das APIs;
        o resultado é retornado quando a vez da requisição chegar.

        Args:
            historico: histórico de mensagens do usuário (isolado por usuário).
            nova_mensagem: mensagem atual enviada pelo usuário.
            usuario_id: identificador do usuário (opcional), usado para
                redução automática de contexto em caso de erro.

        Returns:
            Texto de resposta gerado pela IA.

        Raises:
            ErroDeIA: se nenhum provedor conseguir responder.
        """
        self._garantir_worker_ativo()

        future: "asyncio.Future[str]" = asyncio.get_event_loop().create_future()
        requisicao = _RequisicaoIA(
            historico=historico,
            nova_mensagem=nova_mensagem,
            usuario_id=usuario_id,
            future=future,
        )

        try:
            self._fila.put_nowait(requisicao)
        except asyncio.QueueFull as exc:
            raise ErroDeIA("Fila de requisições à IA está cheia. Tente novamente em instantes.") from exc

        return await future

    def status_provedores(self) -> dict[str, str]:
        """Retorna o status atual (para o comando /health) de cada provedor configurado."""
        status: dict[str, str] = {}
        if self._gemini_disponivel:
            status["gemini"] = self._circuit_breaker.status("gemini")
        if self._groq_disponivel:
            status["groq"] = self._circuit_breaker.status("groq")
        return status

    # ------------------------------------------------------------------
    # Fila assíncrona (worker)
    # ------------------------------------------------------------------

    def _garantir_worker_ativo(self) -> None:
        """Inicia o worker da fila na primeira chamada (requer loop em execução)."""
        if not self._worker_iniciado:
            asyncio.create_task(self._worker_loop())
            self._worker_iniciado = True

    async def _worker_loop(self) -> None:
        """Processa a fila em ordem, uma requisição de cada vez."""
        while True:
            requisicao = await self._fila.get()
            try:
                resposta = await self._gerar_com_fallback(
                    requisicao.historico, requisicao.nova_mensagem, requisicao.usuario_id
                )
                if not requisicao.future.done():
                    requisicao.future.set_result(resposta)
            except Exception as exc:  # noqa: BLE001 - nunca deixar o worker morrer
                if not requisicao.future.done():
                    requisicao.future.set_exception(exc)
            finally:
                self._fila.task_done()

    # ------------------------------------------------------------------
    # Orquestração entre provedores (com retry + circuit breaker)
    # ------------------------------------------------------------------

    async def _gerar_com_fallback(
        self, historico: list[dict[str, str]], nova_mensagem: str, usuario_id: int | None
    ) -> str:
        erros: list[str] = []
        motivo_fallback = ""

        if self._gemini_disponivel:
            if self._circuit_breaker.disponivel("gemini"):
                inicio = time.monotonic()
                try:
                    resposta = await self._gerar_com_retry(
                        "gemini", self._gerar_com_gemini, historico, nova_mensagem, usuario_id
                    )
                    self._circuit_breaker.registrar_sucesso("gemini")
                    await self._registrar("respostas_gemini")
                    duracao_ms = (time.monotonic() - inicio) * 1000
                    log.requisicao_ia("gemini", duracao_ms, duracao_ms, len(historico))
                    return resposta
                except Exception as exc:  # noqa: BLE001
                    self._circuit_breaker.registrar_falha("gemini")
                    await self._registrar("erros_gemini")
                    log.erro("Falha no Gemini, tentando fallback (Groq)", exc)
                    erros.append(f"Gemini: {exc}")
                    motivo_fallback = f"gemini_falhou:{type(exc).__name__}"
            else:
                motivo_fallback = "gemini_circuit_breaker_aberto"
                log.aviso("Gemini está temporariamente indisponível (circuit breaker aberto), usando Groq.")

        if self._groq_disponivel:
            if self._circuit_breaker.disponivel("groq"):
                inicio = time.monotonic()
                try:
                    resposta = await self._gerar_com_retry(
                        "groq", self._gerar_com_groq, historico, nova_mensagem, usuario_id
                    )
                    self._circuit_breaker.registrar_sucesso("groq")
                    await self._registrar("respostas_groq")
                    if motivo_fallback:
                        await self._registrar("respostas_fallback")
                    duracao_ms = (time.monotonic() - inicio) * 1000
                    log.requisicao_ia("groq", duracao_ms, duracao_ms, len(historico), motivo_fallback)
                    return resposta
                except Exception as exc:  # noqa: BLE001
                    self._circuit_breaker.registrar_falha("groq")
                    await self._registrar("erros_groq")
                    log.erro("Falha no Groq", exc)
                    erros.append(f"Groq: {exc}")
            else:
                log.aviso("Groq também está temporariamente indisponível (circuit breaker aberto).")

        raise ErroDeIA("Nenhum provedor de IA conseguiu responder. Detalhes: " + " | ".join(erros))

    async def _gerar_com_retry(
        self,
        provedor: str,
        funcao_geracao: Callable[[list[dict[str, str]], str], Awaitable[str]],
        historico: list[dict[str, str]],
        nova_mensagem: str,
        usuario_id: int | None,
    ) -> str:
        """
        Executa a função de geração com retry e backoff exponencial para
        erros temporários, e redução automática de contexto para erros
        de "contexto muito grande".
        """
        historico_atual = historico
        ultima_excecao: Exception | None = None

        for tentativa in range(1, self._config.retry_max_tentativas + 1):
            try:
                return await funcao_geracao(historico_atual, nova_mensagem)
            except Exception as exc:  # noqa: BLE001
                ultima_excecao = exc

                if eh_erro_contexto_muito_grande(exc):
                    log.aviso(f"[{provedor}] contexto muito grande, reduzindo e tentando novamente.")
                    if usuario_id is not None and self._memoria is not None:
                        self._memoria.reduzir_contexto_temporario(usuario_id)
                        historico_atual = self._memoria.obter_historico(usuario_id)
                    else:
                        metade = max(2, len(historico_atual) // 2)
                        historico_atual = historico_atual[-metade:]
                    continue  # tenta de novo imediatamente, sem contar como falha de rede

                if not eh_erro_retentavel(exc) or tentativa == self._config.retry_max_tentativas:
                    raise

                espera = calcular_backoff(
                    tentativa,
                    self._config.retry_backoff_base_segundos,
                    self._config.retry_backoff_teto_segundos,
                )
                log.aviso(
                    f"[{provedor}] tentativa {tentativa}/{self._config.retry_max_tentativas} falhou "
                    f"({type(exc).__name__}), tentando novamente em {espera:.1f}s."
                )
                await asyncio.sleep(espera)

        assert ultima_excecao is not None
        raise ultima_excecao

    async def _registrar(self, chave: str) -> None:
        """Registra uma estatística, se um callback de estatísticas foi configurado."""
        if self._registrar_estatistica is not None:
            await self._registrar_estatistica(chave)

    # ------------------------------------------------------------------
        # ------------------------------------------------------------------
    # Implementações específicas de cada provedor
    # ------------------------------------------------------------------

    async def _gerar_com_gemini(
        self,
        historico: list[dict[str, str]],
        nova_mensagem: str,
    ) -> str:
        """Gera resposta usando o novo SDK google-genai."""

        def _chamada() -> str:
            conversa = ""

            for msg in historico:
                if msg["role"] == "user":
                    conversa += f"Usuário: {msg['content']}\n"
                else:
                    conversa += f"Assistente: {msg['content']}\n"

            conversa += f"Usuário: {nova_mensagem}"

            resposta = self._cliente_gemini.models.generate_content(
                model=self._config.model_name,
                contents=conversa,
                config=types.GenerateContentConfig(
                   system_instruction=SYSTEM_PROMPT,
                   temperature=self._config.temperature,
                )
            )

            return (resposta.text or "").strip()

        return await asyncio.to_thread(_chamada)
        
    async def _gerar_com_groq(self, historico: list[dict[str, str]], nova_mensagem: str) -> str:
        """Gera resposta usando a API da Groq, como fallback."""

        def _chamada_sincrona() -> str:
            mensagens = [{"role": "system", "content": SYSTEM_PROMPT}]
            mensagens.extend(historico)
            mensagens.append({"role": "user", "content": nova_mensagem})

            resposta = self._cliente_groq.chat.completions.create(
                model=self._config.groq_model_name,
                messages=mensagens,
                temperature=self._config.temperature,
            )
            return resposta.choices[0].message.content.strip()

        return await asyncio.to_thread(_chamada_sincrona)
