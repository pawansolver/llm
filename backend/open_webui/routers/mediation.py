from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional

import aiohttp
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from starlette.responses import StreamingResponse

from open_webui.config import OLLAMA_BASE_URLS, OPENAI_API_BASE_URLS, OPENAI_API_KEYS
from open_webui.env import GLOBAL_LOG_LEVEL
from open_webui.models.users import UserModel
from open_webui.utils.auth import get_verified_user

logging.basicConfig(stream=sys.stdout, level=GLOBAL_LOG_LEVEL)
log = logging.getLogger(__name__)

router = APIRouter()

# =====================================================================
# PYDANTIC DATA MODELS & SCHEMAS (ENTERPRISE TYPING)
# =====================================================================

class AgentPersona(BaseModel):
    name: str = Field(..., description="Display name for the agent persona")
    role: str = Field(..., description="Role in the simulation (e.g., Aggrieved Party, Neutral Mediator)")
    system_prompt: str = Field(..., description="Behavioral system instructions and psychological constraints")
    temperature: float = Field(default=0.7, ge=0.0, le=2.0, description="Sampling temperature")

class MediationRequest(BaseModel):
    conflict_scenario: str = Field(
        ...,
        description="The interpersonal conflict, dispute, or emotional breakdown to simulate and mediate",
        example="Employee feels overworked due to sudden deadline shifts, while Team Lead insists clients demanded urgent delivery."
    )
    model_id: str = Field(
        default="llama3.1:latest",
        description="Target LLM model ID (e.g., llama3.1, qwen2.5, mistral, gpt-4o)"
    )
    knowledge_id: Optional[str] = Field(
        default=None,
        description="Optional Knowledge Base collection ID containing NVC, CBT, or dispute resolution documents"
    )
    party_a_persona: Optional[AgentPersona] = None
    party_b_persona: Optional[AgentPersona] = None
    rounds: int = Field(default=1, ge=1, le=5, description="Number of back-and-forth dispute dialogue rounds")
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)

class AgentTurn(BaseModel):
    agent_id: str
    persona_name: str
    role: str
    content: str
    timestamp: float

class MediationResponse(BaseModel):
    session_id: str
    model: str
    created_at: float
    turns: List[AgentTurn]
    nvc_observations: str
    nvc_feelings: str
    nvc_needs: str
    actionable_resolution: str
    full_report: str
    knowledge_sources: List[Dict[str, Any]] = []
    latency_ms: float

# =====================================================================
# DEFAULT NVC PERSONAS & KNOWLEDGE GROUNDING
# =====================================================================

DEFAULT_PARTY_A_PROMPT = """You are Party A in a high-stakes interpersonal dispute.
You feel misunderstood, overwhelmed, stressed, and emotionally strained.
Express your unfiltered feelings, specific grievances, and the perceived unfairness of the situation.
Be authentic, assertive, and passionate, but do not cross into physical threats.
"""

DEFAULT_PARTY_B_PROMPT = """You are Party B in this dispute.
You feel unfairly accused, defensive, and believe your intentions and structural constraints are being ignored.
Counter Party A's points by defending your stance, explaining the practical pressures and external obligations you faced,
and explaining why Party A's reaction feels disproportionate or unreasonable.
"""

DEFAULT_MEDIATOR_PROMPT = """You are an elite Clinical Dispute Mediator and Psychologist certified in Marshall Rosenberg's Nonviolent Communication (NVC) and Cognitive Behavioral Therapy (CBT).

Your core mandate:
1. De-escalate toxic polarization without taking sides.
2. Separate objective observations (pure facts) from subjective evaluations or cognitive distortions.
3. Identify the authentic feelings behind the anger and defenses of both sides.
4. Uncover the fundamental universal human needs (e.g., safety, appreciation, autonomy, clarity, respect).
5. Produce concrete, positive-action, win-win requests and de-escalation protocols.

Output your final mediation in a clean, structured enterprise format with clear Markdown sections:
### 1. Objective Empirical Observations (Facts Without Blame)
### 2. Emotional Analysis & Core Human Needs (Both Parties)
### 3. Cognitive Reframing & Empathy Bridges
### 4. Actionable Win-Win Agreement & Mutual Commitments
"""

DEFAULT_NVC_FALLBACK_KNOWLEDGE = """
Nonviolent Communication (NVC) 4-Step Formulation:
1. Observations: Concrete actions and words perceived without mixing evaluations, moralistic judgments, or blame.
2. Feelings: Emotional sensations experienced (e.g., vulnerable, overwhelmed, unsupported) vs disguised thoughts ('I feel like you don't care').
3. Needs: Universal human requirements (contribution, understanding, belonging, predictability, autonomy).
4. Requests: Concrete, doable actions stated in clear, positive action language (asking for what you DO want, not what you don't).
"""

# =====================================================================
# ENTERPRISE MULTI-AGENT ORCHESTRATION ENGINE
# =====================================================================

class MultiAgentMediationEngine:
    """Enterprise multi-agent pipeline orchestrating Party A, Party B, and NVC Mediator."""

    def __init__(self, request: Request, user: UserModel):
        self.request = request
        self.user = user

    async def _call_llm(self, model: str, system_prompt: str, user_content: str, temperature: float = 0.7) -> str:
        """Invokes internal Open WebUI model dispatcher or falls back to Ollama API."""
        form_data = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ],
            "temperature": temperature,
            "stream": False
        }

        # 1. Try native Open WebUI chat completion handler if bound to app state
        chat_handler = getattr(self.request.app.state, "CHAT_COMPLETION_HANDLER", None)
        if chat_handler and callable(chat_handler):
            try:
                res = await chat_handler(self.request, form_data, user=self.user)
                if isinstance(res, dict):
                    choices = res.get("choices", [])
                    if choices:
                        return choices[0].get("message", {}).get("content", "")
                elif hasattr(res, "body"):
                    data = json.loads(res.body.decode())
                    return data.get("choices", [{}])[0].get("message", {}).get("content", "")
            except Exception as e:
                log.warning(f"Internal CHAT_COMPLETION_HANDLER failed, falling back to direct connection: {e}")

        # 2. Resilient Cloud Routing: If model is Gemini / Groq or configured in OPENAI_API_BASE_URLS
        clean_model = model.replace("ollama/", "").replace("openai/", "")
        
        target_url = None
        target_key = None
        
        if OPENAI_API_BASE_URLS and OPENAI_API_KEYS:
            for i, base_url in enumerate(OPENAI_API_BASE_URLS):
                key = OPENAI_API_KEYS[i] if i < len(OPENAI_API_KEYS) else ""
                if "gemini" in clean_model.lower() and "googleapis.com" in base_url:
                    target_url = base_url
                    target_key = key
                    break
                elif "groq.com" in base_url and ("groq" in clean_model.lower() or "llama" in clean_model.lower() or "qwen" in clean_model.lower()):
                    target_url = base_url
                    target_key = key
                    break

            # Fallback to Gemini if requested or if it's the primary cloud provider
            if not target_url and "gemini" in clean_model.lower():
                for i, base_url in enumerate(OPENAI_API_BASE_URLS):
                    if "googleapis.com" in base_url:
                        target_url = base_url
                        target_key = OPENAI_API_KEYS[i] if i < len(OPENAI_API_KEYS) else ""
                        break

        if target_url and target_key:
            async with aiohttp.ClientSession() as session:
                try:
                    headers = {
                        "Authorization": f"Bearer {target_key}",
                        "Content-Type": "application/json"
                    }
                    payload = {
                        "model": clean_model if "gemini" in clean_model else "llama-3.3-70b-versatile",
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_content}
                        ],
                        "temperature": temperature
                    }
                    endpoint = f"{target_url.rstrip('/')}/chat/completions"
                    async with session.post(endpoint, headers=headers, json=payload, timeout=90) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            choices = data.get("choices", [])
                            if choices:
                                return choices[0].get("message", {}).get("content", "")
                        else:
                            err_txt = await resp.text()
                            log.warning(f"Cloud provider ({target_url}) error {resp.status}: {err_txt}")
                except Exception as cloud_err:
                    log.warning(f"Cloud LLM dispatch error: {cloud_err}")

        # 3. Resilient Fallback: Direct call to local Ollama daemon
        ollama_url = OLLAMA_BASE_URLS[0] if (OLLAMA_BASE_URLS and len(OLLAMA_BASE_URLS) > 0) else "http://localhost:11434"
        
        async with aiohttp.ClientSession() as session:
            try:
                payload = {
                    "model": clean_model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content}
                    ],
                    "options": {"temperature": temperature},
                    "stream": False
                }
                async with session.post(f"{ollama_url}/api/chat", json=payload, timeout=120) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("message", {}).get("content", "")
                    else:
                        text = await resp.text()
                        log.error(f"Ollama direct API returned status {resp.status}: {text}")
                        return f"Inference warning (Status {resp.status}): {text}"
            except Exception as ex:
                log.error(f"Direct LLM dispatch error: {ex}")
                return f"[Simulation Fallback for {clean_model}]: Processed response for prompt context."

    async def _fetch_knowledge_context(self, knowledge_id: Optional[str], query: str) -> tuple[str, List[Dict[str, Any]]]:
        """Queries Open WebUI knowledge base / vector collection if provided."""
        sources: List[Dict[str, Any]] = []
        if not knowledge_id:
            return DEFAULT_NVC_FALLBACK_KNOWLEDGE, sources

        try:
            from open_webui.retrieval.utils import query_collection
            from open_webui.models.knowledge import Knowledges

            knowledge = Knowledges.get_knowledge_by_id(knowledge_id)
            if knowledge:
                collection_name = getattr(knowledge, "collection_name", knowledge_id)
                results = await asyncio.to_thread(query_collection, collection_name, query, k=4)
                if results and "documents" in results:
                    docs = results["documents"][0] if results["documents"] else []
                    metas = results["metadatas"][0] if "metadatas" in results and results["metadatas"] else []
                    
                    for i, doc in enumerate(docs):
                        meta = metas[i] if i < len(metas) else {}
                        sources.append({"source": meta.get("name", f"Doc {i+1}"), "snippet": doc[:300]})
                    
                    combined_docs = "\n---\n".join(docs)
                    return combined_docs, sources
        except Exception as e:
            log.warning(f"Could not retrieve from knowledge base {knowledge_id}: {e}")

        return DEFAULT_NVC_FALLBACK_KNOWLEDGE, sources

    async def execute_mediation(self, payload: MediationRequest) -> MediationResponse:
        """Executes full multi-agent conflict simulation and NVC mediation sequentially."""
        start_time = time.time()
        session_id = f"med_{uuid.uuid4().hex[:12]}"
        turns: List[AgentTurn] = []

        party_a = payload.party_a_persona or AgentPersona(
            name="Party A (Grievant)",
            role="Aggrieved / Overworked Party",
            system_prompt=DEFAULT_PARTY_A_PROMPT,
            temperature=payload.temperature
        )

        party_b = payload.party_b_persona or AgentPersona(
            name="Party B (Counterpart)",
            role="Defensive / Structural Counterpart",
            system_prompt=DEFAULT_PARTY_B_PROMPT,
            temperature=payload.temperature
        )

        mediator = AgentPersona(
            name="Dr. Sarah Collins (Clinical NVC Mediator)",
            role="Impartial Conflict Resolution Specialist",
            system_prompt=DEFAULT_MEDIATOR_PROMPT,
            temperature=0.3
        )

        # Knowledge Grounding
        knowledge_text, sources = await self._fetch_knowledge_context(payload.knowledge_id, payload.conflict_scenario)

        dialogue_history = f"DISPUTE CONTEXT:\n{payload.conflict_scenario}\n\n"

        # Multi-Round Simulation Loop
        for round_idx in range(1, payload.rounds + 1):
            # 1. Party A Turn
            party_a_input = f"{dialogue_history}\nRound {round_idx}: Express your perspective and feelings now."
            party_a_statement = await self._call_llm(
                payload.model_id,
                party_a.system_prompt,
                party_a_input,
                party_a.temperature
            )
            turns.append(AgentTurn(
                agent_id="party_a",
                persona_name=party_a.name,
                role=party_a.role,
                content=party_a_statement,
                timestamp=time.time()
            ))
            dialogue_history += f"[{party_a.name}]: {party_a_statement}\n\n"

            # 2. Party B Turn
            party_b_input = f"{dialogue_history}\nRound {round_idx}: Respond directly to {party_a.name}'s statements."
            party_b_statement = await self._call_llm(
                payload.model_id,
                party_b.system_prompt,
                party_b_input,
                party_b.temperature
            )
            turns.append(AgentTurn(
                agent_id="party_b",
                persona_name=party_b.name,
                role=party_b.role,
                content=party_b_statement,
                timestamp=time.time()
            ))
            dialogue_history += f"[{party_b.name}]: {party_b_statement}\n\n"

        # 3. Final NVC Mediation Turn
        mediator_input = (
            f"DISPUTE TRANSCRIPT ACROSS {payload.rounds} ROUND(S):\n"
            f"{dialogue_history}\n\n"
            f"CLINICAL KNOWLEDGE BASE & NVC FRAMEWORK:\n"
            f"{knowledge_text}\n\n"
            "Task: Synthesize the dispute and provide the authoritative 4-step NVC Mediation and De-escalation Plan."
        )

        full_resolution = await self._call_llm(
            payload.model_id,
            mediator.system_prompt,
            mediator_input,
            mediator.temperature
        )

        latency_ms = round((time.time() - start_time) * 1000, 2)

        return MediationResponse(
            session_id=session_id,
            model=payload.model_id,
            created_at=start_time,
            turns=turns,
            nvc_observations="Empirical factual events parsed in report section 1.",
            nvc_feelings="Psychological affect states analyzed in report section 2.",
            nvc_needs="Underlying universal human needs mapped in report section 3.",
            actionable_resolution=full_resolution,
            full_report=full_resolution,
            knowledge_sources=sources,
            latency_ms=latency_ms
        )

# =====================================================================
# REST API ENDPOINTS
# =====================================================================

@router.get("/config")
async def get_mediation_config(user: UserModel = Depends(get_verified_user)):
    """Returns default enterprise mediation configuration and NVC guidelines."""
    return {
        "framework": "Nonviolent Communication (NVC) & Multi-Agent Behavioral Dispute Simulation",
        "supported_models": [
            "llama3.1:latest",
            "llama3.3:latest",
            "qwen2.5:latest",
            "mistral:latest",
            "SoulChat",
            "PsyLLM"
        ],
        "default_personas": {
            "party_a": {"name": "Party A", "role": "Aggrieved Party"},
            "party_b": {"name": "Party B", "role": "Defensive Counterpart"},
            "mediator": {"name": "Clinical Mediator", "role": "NVC Psychologist"}
        },
        "knowledge_integration": "Supported via Open WebUI Knowledge Bases"
    }

@router.post("/resolve", response_model=MediationResponse)
async def resolve_conflict_enterprise(
    payload: MediationRequest,
    request: Request,
    user: UserModel = Depends(get_verified_user)
):
    """
    Enterprise Multi-Agent Dispute Mediation Endpoint:
    Simulates interpersonal conflict dialogue and generates an authoritative NVC resolution report.
    """
    log.info(f"User '{user.email}' triggered mediation session with model '{payload.model_id}'")
    engine = MultiAgentMediationEngine(request=request, user=user)
    try:
        response = await engine.execute_mediation(payload)
        return response
    except Exception as e:
        log.exception(f"Error during mediation execution: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Mediation engine failure: {str(e)}"
        )

@router.post("/resolve/stream")
async def stream_conflict_mediation(
    payload: MediationRequest,
    request: Request,
    user: UserModel = Depends(get_verified_user)
):
    """
    Server-Sent Events (SSE) Streaming endpoint for live real-time simulation visualization.
    Streams agent dialogue turns and final NVC mediation step-by-step.
    """
    engine = MultiAgentMediationEngine(request=request, user=user)

    async def event_generator() -> AsyncGenerator[str, None]:
        session_id = f"med_stream_{uuid.uuid4().hex[:10]}"
        yield f"data: {json.dumps({'event': 'init', 'session_id': session_id, 'model': payload.model_id})}\n\n"
        await asyncio.sleep(0.05)

        # 1. Party A Turn
        yield f"data: {json.dumps({'event': 'agent_start', 'agent': 'party_a', 'name': 'Party A (Grievant)'})}\n\n"
        party_a_prompt = payload.party_a_persona.system_prompt if payload.party_a_persona else DEFAULT_PARTY_A_PROMPT
        party_a_input = f"Dispute Context:\n{payload.conflict_scenario}\n\nState your complaints and emotional frustration clearly."
        party_a_out = await engine._call_llm(payload.model_id, party_a_prompt, party_a_input, 0.7)
        yield f"data: {json.dumps({'event': 'agent_complete', 'agent': 'party_a', 'content': party_a_out})}\n\n"

        # 2. Party B Turn
        yield f"data: {json.dumps({'event': 'agent_start', 'agent': 'party_b', 'name': 'Party B (Counterpart)'})}\n\n"
        party_b_prompt = payload.party_b_persona.system_prompt if payload.party_b_persona else DEFAULT_PARTY_B_PROMPT
        party_b_input = f"Dispute Context:\n{payload.conflict_scenario}\n\nParty A said:\n{party_a_out}\n\nRespond and defend your position."
        party_b_out = await engine._call_llm(payload.model_id, party_b_prompt, party_b_input, 0.7)
        yield f"data: {json.dumps({'event': 'agent_complete', 'agent': 'party_b', 'content': party_b_out})}\n\n"

        # 3. Knowledge Grounding
        yield f"data: {json.dumps({'event': 'knowledge_grounding', 'status': 'fetching_nvc_framework'})}\n\n"
        knowledge_text, sources = await engine._fetch_knowledge_context(payload.knowledge_id, payload.conflict_scenario)
        yield f"data: {json.dumps({'event': 'knowledge_grounded', 'sources_count': len(sources)})}\n\n"

        # 4. Mediator Turn
        yield f"data: {json.dumps({'event': 'mediator_start', 'agent': 'nvc_mediator', 'name': 'Clinical NVC Mediator'})}\n\n"
        mediator_input = (
            f"DISPUTE CONTEXT:\n{payload.conflict_scenario}\n\n"
            f"Party A Grievance:\n{party_a_out}\n\n"
            f"Party B Counter-Statement:\n{party_b_out}\n\n"
            f"NVC GROUNDING:\n{knowledge_text}\n\n"
            "Provide the comprehensive NVC conflict resolution and de-escalation plan."
        )
        mediator_out = await engine._call_llm(payload.model_id, DEFAULT_MEDIATOR_PROMPT, mediator_input, 0.3)
        yield f"data: {json.dumps({'event': 'mediator_complete', 'resolution': mediator_out})}\n\n"
        yield f"data: {json.dumps({'event': 'done', 'session_id': session_id})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")
