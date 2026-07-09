"""
================================================================================
WEATHER + OUTFIT ADVISOR AGENT -- FULLY ANNOTATED VERSION
================================================================================
This file is a working custom AI agent AND a study reference. Every section
is commented with the exact concept it demonstrates from the Feynman-style
lesson on Custom Agent Creation. Read top to bottom like notes.

THE 4 CORE INGREDIENTS OF A CUSTOM AGENT (recap):
  1. BRAIN     -- a generalist AI model, wrapped with a role
  2. RULES     -- fixed text instructions (the system prompt)
  3. TOOLS     -- functions the agent can request, never runs itself
  4. MEMORY    -- notes saved outside the AI, selectively, re-inserted later

PLUS a 5th piece added after two real bugs surfaced during testing:
  5. GUARDRAILS -- CODE-level checks (e.g. is_valid_city()) that
     validate the model's output regardless of what it claims. Prompt
     rules are a request; guardrails are enforcement.

KEY INSIGHT:
  The AI is stateless and "hand-less." It can only generate text.
  Every capability below -- calling functions, remembering things,
  staying in scope -- is built by CODE AROUND the AI, not by the AI itself.

--------------------------------------------------------------------------------
FILE MAP -- functions grouped by responsibility (read in this order)
--------------------------------------------------------------------------------

  [1] CONFIGURATION & BRAIN CONNECTION
        INPUT_MAX_LENGTH, BASE_URL, MODEL, API_KEY, client

  [2] RULES (prompt-based, soft enforcement)
        SYSTEM_PROMPT

  [3] GUARDRAILS (code-based, hard enforcement)
        is_valid_city          -- validate city strings from the model
        is_geography_question  -- INPUT:  block off-topic geography asks
        is_geography_reply     -- OUTPUT: catch geography facts in replies
        is_list_reply          -- OUTPUT: catch city-directory list leaks
        OFF_TOPIC_REFUSAL      -- standard redirect message (never lock out)

  [4] PERSISTENT MEMORY (survives after program closes)
        load_memory, save_memory

  [5] BRAIN -- LLM interface
        call_llm               -- single gateway to the model (no native tools)

  [6] TOOLS -- Geocoding (city name -> coordinates)
        _format_location_label, _normalize_geocode_result
        geocode_city, _pick_from_query, resolve_city

  [7] TOOLS -- Weather (coordinates -> live conditions)
        fetch_weather_for_location, fetch_weather

  [8] THINK -- manual "tool request" steps (ReAct: reasoning before acting)
        extract_city           -- stage 1a: explicit city in this message
        infer_city             -- stage 1b: inferred city from chat context

  [9] REACT ORCHESTRATION -- one full turn (think -> act -> observe -> answer)
        _reply_with_weather           -- act + observe + answer for a city
        continue_after_disambiguation -- resume after user picks a location
        continue_after_city_confirmation -- resume after yes/no on inferred city
        run_turn                      -- main entry: one user message in

 [10] CLI ENTRY POINT (terminal only; web uses app.py instead)
        _cli_finalize_turn       -- handle pause states in the terminal
        __main__                 -- startup + conversation loop
================================================================================
"""

import re
import requests
import json
import os
from openai import OpenAI
from dotenv import load_dotenv


# =============================================================================
# [1] CONFIGURATION & BRAIN CONNECTION
# =============================================================================
# The "brain" is NOT retrained here -- we only configure HOW to reach it.
# All connection details live in .env so the same code works across machines
# and providers (vLLM, Ollama, cloud APIs). Only the client plumbing changes.
# -----------------------------------------------------------------------------

INPUT_MAX_LENGTH = 500  # INPUT GUARDRAIL: cap prompt size before any LLM call

load_dotenv()

BASE_URL = os.getenv("BASE_URL")
MODEL = os.getenv("MODEL")
API_KEY = os.getenv("API_KEY", "not-needed")

client = OpenAI(base_url=BASE_URL, api_key=API_KEY)


# =============================================================================
# [2] RULES -- prompt-based instructions (SOFT enforcement)
# =============================================================================
# Plain text the model reads every turn via chat_history[0]. Shapes a
# generalist into an outfit advisor. These are REQUESTS, not guarantees --
# see [3] GUARDRAILS for hard enforcement when the model ignores rules.
#
# Real bugs that motivated specific rules here:
#   - Scope leak    -> explicit "refuse off-topic" rule
#   - Hallucination -> "NEVER invent weather numbers" rule
#   - City lists    -> "do NOT list or suggest cities" rule (still bypassed
#                      sometimes -- hence is_list_reply() in guardrails)
# -----------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a friendly Outfit Advisor agent.

Your ONLY job: help the user decide what to wear based on current weather.

Rules:
- Give practical, specific clothing suggestions (not generic "dress appropriately").
- If it's raining, always mention an umbrella or raincoat.
- If it's cold (<15C), suggest layers.
- If it's hot (>28C), suggest light, breathable clothing.
- Keep your final answer short and friendly -- 2-4 sentences.
- If the user asks anything NOT related to weather or outfits (e.g. news,
  politics, general knowledge, coding help), politely decline and redirect
  them back to outfit/weather help. Do NOT answer the off-topic question,
  even partially. Example: "I'm just your outfit advisor, so I can't help
  with that -- but I'd love to help you figure out what to wear today!"
- CRITICAL: NEVER invent, guess, or make up specific weather numbers
  (temperature, rain, wind). Only state weather figures that appear in a
  "[System note: current weather data -> ...]" message in THIS conversation.
  If you don't have that data for the city being discussed, say you need
  to check first instead of stating any number.
- If you don't know which city the user means, simply ask them to name
  ONE specific city. Do NOT list, suggest, or name possible cities
  yourself (e.g. do not say "some cities in that country are..."), even
  if you believe it's helping narrow things down. Providing that kind of
  factual/geographic information is out of scope, no matter the reason.
"""


# =============================================================================
# [3] GUARDRAILS -- code-based checks (HARD enforcement)
# =============================================================================
# Python logic the model cannot talk its way around. Every guardrail that
# blocks or rewrites also REDIRECTS the user (refusal + "What's your city?")
# so the conversation never dead-ends.
# -----------------------------------------------------------------------------


def is_valid_city(candidate: str) -> bool:
    """
  GUARDRAIL -- extraction validation.

  Applied to outputs of extract_city() and infer_city() BEFORE treating
  text as a city name. Rejects digits, garbage, and math answers that
  slipped through mini-prompts (e.g. "5 times 5 is 25." treated as a city).

  Returns True only for plausible place-name strings.
  """
    if not candidate or candidate.upper() == "NONE":
        return False
    if len(candidate) > 100:  # Reject unreasonably long city names
        return False
    if any(ch.isdigit() for ch in candidate):
        return False
    # allow letters, spaces, hyphens, apostrophes, periods, commas (e.g. "Queenstown, New Zealand")
    if not re.fullmatch(r"[\w\s\-'\.,]+", candidate, re.UNICODE):
        return False
    return True


def is_geography_question(text: str) -> bool:
    """
  GUARDRAIL -- INPUT (before LLM acts).

  Detects general-knowledge geography questions ("what country is X in?",
  "where is Y?") via regex patterns. Used at the start of run_turn() so
  off-topic asks never reach tool-calling or main chat generation.
  """
    lower = text.lower()
    patterns = [
        r"\bwhat\s+countr",
        r"\bwhat\s+cou?t",  # catches typo "coutry"
        r"\bwhich\s+countr",
        r"\bwhat\s+\w+\s+city\s+is\b",
        r"\bwhich\s+\w+\s+city\s+is\b",
        r"\bwhat\s+city\s+is\b",
        r"\bwhich\s+city\s+is\b",
        r"\bwhere\s+is\b",
        r"\bwhere\s+are\b",
        r"\bin\s+what\s+countr",
        r"\bwhat\s+continent\b",
        r"\bwhich\s+state\b",
        r"\bwhat\s+state\b",
        r"\bpopulation\s+of\b",
        r"\bcapital\s+of\b",
        r"\bhow\s+far\b",
        r"\blocated\s+in\b",
    ]
    return any(re.search(p, lower) for p in patterns)


def is_geography_reply(text: str) -> bool:
    """
  GUARDRAIL -- OUTPUT (after LLM replies, no-tool path).

  Catches geography facts the model produced despite SYSTEM_PROMPT rules
  (e.g. "Pindi is a city in Pakistan"). Pattern-matches the reply shape,
  not the user's phrasing -- closes prompt-rule bypasses.
  """
    lower = text.lower()
    patterns = [
        r"\bis\s+a\s+city\s+in\b",
        r"\bis\s+located\s+in\b",
        r"\bis\s+in\s+(?:the\s+)?(?:country|nation|state|province|region)\b",
        r"\blocated\s+in\s+(?:the\s+)?(?:country|nation|state|province)\b",
        r"\bcountry\s+(?:is|called)\b",
        r"\bcapital\s+of\b",
        r"\bcontinent\s+(?:is|of)\b",
    ]
    return any(re.search(p, lower) for p in patterns)


# Standard refusal used by geography guardrails -- always ends with a redirect.
OFF_TOPIC_REFUSAL = (
    "I'm just your outfit advisor, so I can't help with geography questions — "
    "but I'd love to help you figure out what to wear! What's your city?"
)


def is_list_reply(text: str) -> bool:
    """
  GUARDRAIL -- OUTPUT (after LLM replies, no-tool path).

  Flags replies that look like a multi-item directory (3+ numbered or
  bulleted lines). The model sometimes lists cities despite prompt rules;
  this checks output SHAPE regardless of how the user asked.
  """
    numbered = re.findall(r"(?m)^\s*\d+[\.\)]\s+\S+", text)
    bulleted = re.findall(r"(?m)^\s*[-*]\s+\S+", text)
    return len(numbered) >= 3 or len(bulleted) >= 3


# =============================================================================
# [4] PERSISTENT MEMORY -- survives after the program closes
# =============================================================================
# The AI has no real long-term memory. We save selective facts to disk and
# re-inject them on the next run. Only last_city is stored -- not the full
# transcript -- demonstrating "sticky notes" not "diary dump."
# -----------------------------------------------------------------------------

MEMORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent_memory.json")


def load_memory() -> dict:
    """Read persistent facts from agent_memory.json. Returns {} if missing."""
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, "r") as f:
            return json.load(f)
    return {}


def save_memory(memory: dict):
    """Write persistent facts to disk. Failures are logged, not raised."""
    try:
        with open(MEMORY_FILE, "w") as f:
            json.dump(memory, f)
    except OSError as e:
        print(f"  [memory warning] could not save memory: {e}")


# =============================================================================
# [5] BRAIN -- LLM interface
# =============================================================================
# Single function that talks to the model. No native tools= parameter --
# this project uses MANUAL tool-calling (see [8] THINK) because the vLLM
# gateway did not parse structured tool_calls reliably for qwen3-coder.
# -----------------------------------------------------------------------------


def call_llm(messages, max_tokens: int = 300):
    """
  Send messages to the LLM and return the assistant's text reply.

  Args:
      messages:    OpenAI-style list of {role, content} dicts.
      max_tokens:  Cap on completion length (short for extraction, longer for advice).

  This is the only place the "brain" is invoked. Everything else in this
  file decides WHAT to put in messages and WHAT to do with the reply.
  """
    response = client.chat.completions.create(
        model=MODEL,
        messages=messages,
        max_tokens=max_tokens,
        timeout=30,
    )
    return response.choices[0].message.content.strip()


# =============================================================================
# [6] TOOLS -- Geocoding (city name -> coordinates + metadata)
# =============================================================================
# The AI never calls these directly. Our orchestration code calls them after
# the THINK steps decide a city name is worth looking up. Geocoding also
# handles AMBIGUITY (multiple Queenstowns) via resolve_city().
# -----------------------------------------------------------------------------

GEO_SEARCH_COUNT = 5  # Max candidates returned when a city name is ambiguous


def _format_location_label(result: dict) -> str:
    """
  Build a human-readable label for UI and memory storage.
  Example: "Queenstown, Otago, New Zealand"
  """
    parts = [result.get("name", "")]
    admin1 = result.get("admin1", "")
    country = result.get("country", "")
    if admin1 and admin1 != parts[0]:
        parts.append(admin1)
    if country:
        parts.append(country)
    return ", ".join(p for p in parts if p)


def _normalize_geocode_result(raw: dict) -> dict:
    """
  Convert one Open-Meteo geocoding API result into our internal location dict.
  Keeps only the fields the rest of the agent needs.
  """
    return {
        "name": raw.get("name", ""),
        "country": raw.get("country", ""),
        "admin1": raw.get("admin1", ""),
        "latitude": raw["latitude"],
        "longitude": raw["longitude"],
        "label": _format_location_label(raw),
    }


def geocode_city(city: str, count: int = GEO_SEARCH_COUNT) -> list[dict] | dict:
    """
  TOOL -- search Open-Meteo geocoding API for matching place names.

  Returns:
      list[dict]  -- zero or more normalized location dicts
      {error: ...} -- on network failure, rate limit, or no results
  """
    city = city[:100]
    try:
        geo_resp = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city, "count": count},
            timeout=10,
        )
        if geo_resp.status_code == 429:
            return {"error": "Rate limited. Please wait a moment and try again."}
        geo_data = geo_resp.json()
        if "results" not in geo_data:
            return {"error": f"Could not find city '{city}'"}
        return [_normalize_geocode_result(r) for r in geo_data["results"]]
    except requests.exceptions.RequestException as e:
        return {"error": f"Network error looking up '{city}': {e}"}


def _pick_from_query(results: list[dict], user_message: str) -> dict | None:
    """
  Auto-resolve ambiguity when the user already named country/region in text.
  Example: "Queenstown, New Zealand" -> pick the NZ result without asking.
  Returns None if message doesn't disambiguate enough.
  """
    msg = user_message.lower()
    for result in results:
        country = result.get("country", "").lower()
        admin1 = result.get("admin1", "").lower()
        if country and country in msg:
            return result
        if admin1 and admin1 in msg:
            return result
    return None


def resolve_city(city: str, user_message: str = "") -> dict:
    """
  TOOL -- resolve a city name to exactly one location, or pause for user input.

  Return shapes:
      {"location": {...}}              -- single match (or auto-picked)
      {"needs_disambiguation": True, ...} -- multiple matches; user must pick
      {"error": "..."}                 -- not found or API failure
  """
    results = geocode_city(city)
    if isinstance(results, dict) and "error" in results:
        return results
    if not results:
        return {"error": f"Could not find city '{city}'"}

    picked = _pick_from_query(results, user_message)
    if picked:
        return {"location": picked}
    if len(results) == 1:
        return {"location": results[0]}

    return {
        "needs_disambiguation": True,
        "city_query": city,
        "locations": results,
        "reply": f'I found multiple places called "{city}". Which one did you mean?',
        "options": [{"index": i, "label": r["label"]} for i, r in enumerate(results)],
    }


# =============================================================================
# [7] TOOLS -- Weather (coordinates -> live conditions)
# =============================================================================
# After geocoding resolves to one location, these fetch real weather data.
# Results are injected into chat_history as a [System note: ...] message
# so the LLM can "observe" tool output before generating outfit advice.
# -----------------------------------------------------------------------------


def fetch_weather_for_location(location: dict) -> dict:
    """
  TOOL -- fetch current weather for a resolved location (lat/lon).

  Returns dict with city label, temperature_C, precipitation_mm, wind_speed_kmh
  or {"error": "..."} on failure.
  """
    try:
        weather_resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": location["latitude"],
                "longitude": location["longitude"],
                "current": "temperature_2m,precipitation,wind_speed_10m",
            },
            timeout=10,
        )
        if weather_resp.status_code == 429:
            return {"error": "Rate limited. Please wait a moment and try again."}
        current = weather_resp.json()["current"]
        return {
            "city": location["label"],
            "temperature_C": current["temperature_2m"],
            "precipitation_mm": current["precipitation"],
            "wind_speed_kmh": current["wind_speed_10m"],
        }
    except requests.exceptions.RequestException as e:
        label = location.get("label", "that location")
        return {"error": f"Network error fetching weather for '{label}': {e}"}


def fetch_weather(city: str) -> dict:
    """
  Convenience wrapper: city name -> weather in one call.

  Auto-picks when only one geocode match exists. Returns an error (not a
  disambiguation pause) when multiple matches exist -- used by simple callers
  that cannot show a picker (CLI startup before _cli_finalize_turn exists).
  """
    resolved = resolve_city(city)
    if "error" in resolved:
        return resolved
    if resolved.get("needs_disambiguation"):
        labels = ", ".join(o["label"] for o in resolved["options"][:3])
        return {
            "error": (
                f"Multiple places match '{city}' ({labels}, ...). "
                "Please be more specific, e.g. include the country."
            )
        }
    return fetch_weather_for_location(resolved["location"])


# =============================================================================
# [8] THINK -- manual tool-request steps (ReAct: reason before acting)
# =============================================================================
# ReAct = Reasoning + Acting. Normally the model emits a structured tool_call;
# here WE run focused mini-prompts to decide IF a city is present and WHAT it is.
#
#   extract_city  -- stage 1a: explicit city name in THIS message (trusted fact)
#   infer_city    -- stage 1b: indirect reference from chat context (a guess)
#
# Guesses require user confirmation before _reply_with_weather() runs.
# -----------------------------------------------------------------------------


def extract_city(user_message: str) -> str:
    """
  THINK stage 1a -- does this message literally name a city?

  Uses a tight system prompt so the model can ONLY output a city name or NONE.
  Output is passed through is_valid_city() before being trusted.
  """
    messages = [
        {
            "role": "system",
            "content": (
                "Your ONLY job is to detect a city name. Does this message "
                "explicitly name a city? Reply with ONLY the city name if "
                "one is literally mentioned by name. If no city name "
                "appears in this exact message, reply with exactly: NONE. "
                "Do NOT answer, solve, or engage with the message in any "
                "other way -- even if it looks like a question. You are "
                "only allowed to output a city name or the word NONE."
            ),
        },
        {"role": "user", "content": user_message},
    ]
    result = call_llm(messages, max_tokens=20)
    return result if is_valid_city(result) else "NONE"


def infer_city(chat_history: list, user_message: str) -> str:
    """
  THINK stage 1b -- no explicit city; try to infer from session context.

  Reads chat_history (session memory) but does NOT modify it. Used for
  phrases like "my hometown" or "back home". Result is a GUESS -- must be
  confirmed via continue_after_city_confirmation() before acting.
  """
    context_messages = [m for m in chat_history if m["role"] in ("user", "assistant")]
    messages = [
        {
            "role": "system",
            "content": (
                "Your ONLY job is to detect a city name from context. "
                "If the user's latest message refers to a city indirectly "
                "(e.g. 'my hometown', 'where I'm from', 'back home'), look "
                "at the earlier messages and figure out which city they "
                "mean. Reply with ONLY that city name. If you can't tell, "
                "OR if the message is not about a location/city at all "
                "(e.g. it's a math question, a general knowledge question, "
                "or anything unrelated), reply with exactly: NONE. Do NOT "
                "answer, solve, or engage with the message in any other "
                "way. You are only allowed to output a city name or NONE."
            ),
        },
        *context_messages,
        {"role": "user", "content": user_message},
    ]
    result = call_llm(messages, max_tokens=20)
    return result if is_valid_city(result) else "NONE"


# =============================================================================
# [9] REACT ORCHESTRATION -- one full turn per user message
# =============================================================================
# Flow: guard -> think -> (confirm?) -> (disambiguate?) -> act -> observe -> answer
#
#   run_turn()                        -- main entry called by CLI and app.py
#   _reply_with_weather()             -- act + observe + answer for a known city
#   continue_after_city_confirmation() -- resume after yes/no on inferred city
#   continue_after_disambiguation()   -- resume after user picks from a list
#
# Return type is str | dict: plain text reply OR a pause dict (needs_confirmation,
# needs_disambiguation) that the caller must handle before the turn is complete.
# -----------------------------------------------------------------------------


def _reply_with_weather(
    chat_history: list,
    memory: dict,
    user_message: str,
    city: str,
    location: dict | None = None,
) -> str | dict:
    """
  ACT + OBSERVE + ANSWER for a confirmed city.

  1. Resolve geocode (unless location already provided after disambiguation)
  2. Save last_city to persistent memory if changed
  3. fetch_weather_for_location()  -- ACT
  4. Inject [System note: weather data] into chat_history  -- OBSERVE
  5. call_llm() for natural-language outfit advice  -- ANSWER
  """
    if location is None:
        resolved = resolve_city(city, user_message)
        if "error" in resolved:
            reply = f"Sorry, I couldn't find '{city}': {resolved['error']}"
            chat_history.append({"role": "user", "content": user_message})
            chat_history.append({"role": "assistant", "content": reply})
            return reply
        if resolved.get("needs_disambiguation"):
            print(f"  [disambiguation] {len(resolved['locations'])} matches for {city!r}")
            return {
                "needs_disambiguation": True,
                "reply": resolved["reply"],
                "options": resolved["options"],
                "locations": resolved["locations"],
                "city_query": city,
                "original_message": user_message,
            }
        location = resolved["location"]

    label = location["label"]
    if memory.get("last_city", "").lower() != label.lower():
        memory["last_city"] = label
        save_memory(memory)
        print(f"  [memory saved] last_city = {label}")

    weather = fetch_weather_for_location(location)
    print(f"  [tool result] {weather}")

    if "error" in weather:
        reply = f"Sorry, I couldn't fetch the weather for {label}: {weather['error']}"
        chat_history.append({"role": "user", "content": user_message})
        chat_history.append({"role": "assistant", "content": reply})
        return reply

    chat_history.append({"role": "user", "content": user_message})
    chat_history.append({
        "role": "user",
        "content": f"[System note: current weather data -> {json.dumps(weather)}]"
    })
    reply = call_llm(chat_history)
    chat_history.append({"role": "assistant", "content": reply})
    return {"reply": reply, "weather": weather}


def continue_after_disambiguation(
    chat_history: list,
    memory: dict,
    original_message: str,
    city_query: str,
    location_index: int,
    locations: list[dict],
) -> str | dict:
    """
  Resume a paused turn after the user picks one location from a list.

  Called by app.py (web button click) or _cli_finalize_turn() (terminal number).
  Delegates to _reply_with_weather() with the chosen location pre-resolved.
  """
    if location_index < 0 or location_index >= len(locations):
        return "Please pick one of the listed cities."
    return _reply_with_weather(
        chat_history,
        memory,
        original_message,
        city_query,
        location=locations[location_index],
    )


def continue_after_city_confirmation(
    chat_history: list,
    memory: dict,
    original_message: str,
    city: str,
    confirmed: bool,
) -> str | dict:
    """
  Resume a paused turn after yes/no on an inferred city name.

  If confirmed -> fetch weather for that city.
  If rejected  -> ask user to name a city explicitly (never lock out).
  """
    if not confirmed:
        chat_history.append({"role": "user", "content": original_message})
        reply = "No problem -- which city would you like the weather for?"
        chat_history.append({"role": "assistant", "content": reply})
        return reply
    return _reply_with_weather(chat_history, memory, original_message, city)


def run_turn(chat_history: list, memory: dict, user_message: str) -> str | dict:
    """
  MAIN ENTRY -- process one user message through the full ReAct loop.

  Called by:
      - CLI conversation loop (weather_agent.py __main__)
      - Flask /api/chat (app.py)

  May return:
      str                          -- final text reply
      dict needs_confirmation      -- pause: user must answer yes/no
      dict needs_disambiguation    -- pause: user must pick a location
      dict with reply + weather    -- final reply plus structured weather (web UI)
  """

    if is_geography_question(user_message):
        print(f"  [guardrail] blocked geography question: {user_message[:60]!r}...")
        chat_history.append({"role": "user", "content": user_message})
        chat_history.append({"role": "assistant", "content": OFF_TOPIC_REFUSAL})
        return OFF_TOPIC_REFUSAL

    # --- THINK, stage 1: explicit fact (trusted, no confirmation needed) ---
    city = extract_city(user_message)
    print(f"  [explicit city] {city!r}")

    # --- THINK, stage 2: inferred guess (must be confirmed before acting) ---
    if city.upper() == "NONE":
        inferred = infer_city(chat_history, user_message)
        print(f"  [inferred city] {inferred!r}")

        if inferred.upper() != "NONE":
            # CONCEPT: confirm before acting on an inference. The agent
            # never silently assumes -- it treats the user as the source
            # of truth, even when it's fairly confident in its guess.
            return {
                "needs_confirmation": True,
                "reply": f"Just to confirm, do you mean {inferred}?",
                "pending_city": inferred,
                "original_message": user_message,
            }

    if city.upper() == "NONE":
        # --- No tool needed at all this turn ---
        # CONCEPT: not every message needs a tool call. The agent should
        # only act when it actually needs to -- otherwise just reply.
        # The anti-hallucination RULE (in SYSTEM_PROMPT) is what stops the
        # model from inventing weather numbers here, since no real data
        # exists in this turn's context.
        chat_history.append({"role": "user", "content": user_message})
        reply = call_llm(chat_history)

        # OUTPUT GUARDRAIL: catch list-of-places leaks regardless of how
        # the request was phrased. Prompt rules alone kept getting
        # bypassed by rewording ("in English", "not multiple languages")
        # -- this checks the actual shape of the output instead.
        if is_list_reply(reply):
            print(f"  [guardrail] blocked list-shaped reply: {reply[:60]!r}...")
            reply = ("I can only help with weather and outfit advice, so I "
                      "can't give you a list like that -- could you tell me "
                      "the specific city you're in?")

        if is_geography_reply(reply):
            print(f"  [guardrail] blocked geography reply: {reply[:60]!r}...")
            reply = OFF_TOPIC_REFUSAL

        chat_history.append({"role": "assistant", "content": reply})
        return reply

    return _reply_with_weather(chat_history, memory, user_message, city)


# =============================================================================
# [10] CLI ENTRY POINT -- terminal interface only
# =============================================================================
# The web UI (app.py) calls run_turn() directly and handles pause states via
# JSON + buttons. The CLI uses _cli_finalize_turn() to prompt in the terminal.
# -----------------------------------------------------------------------------


def _cli_finalize_turn(chat_history: list, memory: dict, result: str | dict) -> str | dict:
    """
  CLI helper -- loop until pause states (confirm / disambiguate) are resolved.

  Web equivalent: app.py stores pending_confirmation / pending_disambiguation
  in the Flask session and resumes on the next /api/chat request.
  """
    while isinstance(result, dict):
        if result.get("needs_confirmation"):
            confirm = input(f"Agent: {result['reply']} (yes/no): ").strip().lower()
            result = continue_after_city_confirmation(
                chat_history,
                memory,
                result["original_message"],
                result["pending_city"],
                confirm in ("yes", "y"),
            )
        elif result.get("needs_disambiguation"):
            print(f"Agent: {result['reply']}")
            for opt in result["options"]:
                print(f"  {opt['index'] + 1}. {opt['label']}")
            while True:
                choice = input("Pick a number: ").strip()
                if choice.isdigit() and 1 <= int(choice) <= len(result["locations"]):
                    result = continue_after_disambiguation(
                        chat_history,
                        memory,
                        result["original_message"],
                        result["city_query"],
                        int(choice) - 1,
                        result["locations"],
                    )
                    break
                print("  Please enter a valid number.")
        else:
            break
    return result


if __name__ == "__main__":

    # Prints on every run so there's never ambiguity about which version
    # of the file is actually executing -- bump this string any time you
    # make a meaningful change, and check it matches what you expect.
    print("=== Outfit Advisor -- build: guardrails-v4 (output-level list detector) ===\n")

    # SESSION MEMORY: this list IS the memory for one running of the
    # program. It lives only in RAM -- created fresh here, and gone
    # forever the moment the script exits. Every turn appends to this
    # SAME list and the WHOLE list is re-sent to the model every time --
    # that's what lets the AI "remember" earlier parts of this chat, even
    # though the AI itself is stateless between individual API calls.
    chat_history = [{"role": "system", "content": SYSTEM_PROMPT}]

    # PERSISTENT MEMORY: read whatever was saved to disk in a PREVIOUS
    # run of this program (or {} if this is the first time ever).
    memory = load_memory()

    print("Outfit Advisor ready (manual tool-calling mode). Type 'quit' to exit.\n")

    # -------------------------------------------------------------------
    # CONCEPT: proactive use of persistent memory, WITH confirmation.
    # The agent notices a saved fact from a past session and OFFERS it --
    # but never silently assumes. This mirrors the "waiter jots a sticky
    # note, doesn't guess your whole order next time" analogy: memory
    # makes the agent faster and more helpful, but the user stays the
    # final authority.
    # -------------------------------------------------------------------
    remembered_city = memory.get("last_city")

    # CONCEPT: the guardrail protects NEW writes, but data saved BEFORE
    # the guardrail existed can still be sitting in memory.json from an
    # earlier, buggier run. Validate on READ too, so corrupted persistent
    # memory self-heals instead of being offered back to the user forever.
    if remembered_city and not is_valid_city(remembered_city):
        print(f"  [memory self-heal] discarding invalid saved city: {remembered_city!r}")
        memory.pop("last_city", None)
        save_memory(memory)
        remembered_city = None

    if remembered_city:
        confirm = input(f"Agent: Are you still in {remembered_city}? (yes/no): ").strip().lower()

        if confirm in ("yes", "y"):
            startup = _reply_with_weather(
                chat_history, memory, f"[remembered city: {remembered_city}]", remembered_city
            )
            startup = _cli_finalize_turn(chat_history, memory, startup)
            if isinstance(startup, dict):
                print("Agent:", startup.get("reply", startup), "\n")
            else:
                print("Agent:", startup, "\n")
        else:
            # Rejected -- do NOT use the stale memory. Just move on.
            print("Agent: No problem! Where are you today?\n")

    # -------------------------------------------------------------------
    # CONCEPT: the outer "conversation loop" -- this is NOT the ReAct
    # loop itself (that's inside run_turn for a single turn); this is
    # the higher-level loop that keeps the whole chat alive turn after
    # turn, reusing the SAME chat_history and memory objects each time.
    # -------------------------------------------------------------------
    while True:
        user_input = input("You: ")
        if user_input.strip().lower() in ("quit", "exit"):
            break
        
        # Input guardrail: prevent runaway prompts
        if len(user_input) > INPUT_MAX_LENGTH:
            print("Agent: Your message is too long. Please keep it under 500 characters.\n")
            continue

        result = run_turn(chat_history, memory, user_input)
        answer = _cli_finalize_turn(chat_history, memory, result)
        if isinstance(answer, dict):
            print("Agent:", answer.get("reply", answer), "\n")
        else:
            print("Agent:", answer, "\n")
