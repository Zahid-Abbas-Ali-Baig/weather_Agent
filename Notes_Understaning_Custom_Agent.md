# Custom Agent Creation — Master Notes

## 🎯 Core Definition

A **custom AI agent** is a generalist AI model shaped into a domain-specific helper using four core ingredients: **Rules, Tools, Memory**, layered on top of a **Brain**. The AI itself never truly *acts* or *remembers* on its own — it only generates text. Every other capability (memory, tool use, staying in scope) is built by **systems around the AI**, not by the AI itself.

---

## 🧩 The 4 Core Ingredients

| Ingredient | Simple idea | What it actually is |
|---|---|---|
| **Brain** | A generalist AI, ready to specialize | The AI model + a hidden "system prompt" defining its role. Provider-agnostic — works the same whether it's Claude via Anthropic's API or a self-hosted model via an OpenAI-compatible gateway (only the JSON plumbing changes) |
| **Rules** | A fixed rulebook for the domain | Plain text sentences in the system prompt. Stay fixed until a human edits them — the AI doesn't rewrite its own rules |
| **Tools** | Ability to *do* things, not just talk | Functions the AI can *request* — it never runs them itself. A separate program executes them and returns the result |
| **Memory** | Remembers preferences over time | The AI has *no real memory*. A program saves notes elsewhere and re-inserts them into future prompts, creating the *illusion* of memory |

**Analogy:** A new employee at a pizza shop — brain = the person, rules = training manual, tools = register/phone, memory = recognizing regulars.

---

## ⚙️ Tool-Calling — Step by Step

1. User sends a request
2. AI checks: *"Do I already know this, or do I need a tool?"* (no rulebook check — just reasoning)
3. If a tool is needed → AI **requests** it (does NOT run it itself)
4. A **separate outside program** actually executes the function
5. Result is passed back to the AI
6. AI turns the raw result into a natural-language reply

**Analogy:** You (the AI) can't cook. You tell the waiter (outside system) what you want; the waiter actually cooks and brings it back. You only ever *request*.

### Tool Selection (when multiple tools exist)
Each tool has a short **description/label**. The AI matches the request against these labels — like reading drawer labels in a toolbox, not inspecting every tool's internals.

---

## 🔁 Multi-Step Tasks — The ReAct Loop

For complex tasks, the agent breaks work into pieces and repeats:

**Think** (what's next?) → **Act** (call one tool) → **Observe** (check result) → **Think again** (done, or another round?) → repeat until complete.

This pattern is called **ReAct (Reasoning + Acting)** in AI research.

**Analogy:** A chef following a recipe — chop (act) → check (observe) → decide next step (think) → sauté (act) → repeat. Not just staring at the recipe over and over.

### Loop Prevention
To stop infinite loops: **max step limits**, **timeouts**, **repeat detection** (same input/output looping), and an **explicit "done" signal** from the AI.

---

## 👥 Multi-Agent Systems

For bigger problems, use a **team of specialized agents** (planner, researcher, booker, writer), coordinated by a **master/orchestrator agent** that decides which agent handles which part and combines the results.

**Analogy:** A restaurant kitchen — prepper, griller, plater, all coordinated by a head chef.

---

## 💾 Memory, In Depth

| Type | Lives where | Survives closing the program? |
|---|---|---|
| **Session memory** | A plain list/array in RAM (e.g. `chat_history`), re-sent to the model every turn | ❌ No — gone the moment the program exits |
| **Persistent memory** | A file or database (e.g. `agent_memory.json`) | ✅ Yes — loaded back in on the next run |
| **Selective memory** | Only specific, relevant facts saved (e.g. just `last_city`, not the whole conversation) | Matches how memory *should* be built — short "sticky notes," not a full transcript dump |

**Key pattern learned by building this:** an agent should never **silently** act on remembered or inferred information. It should **confirm with a yes/no** before using it — whether that's a fact carried over from a previous session ("Are you still in Lahore?") or something inferred mid-conversation from context ("Just to confirm, do you mean Karachi?"). Explicitly stated facts don't need confirmation; inferred or remembered ones do.

---

## 🛡️ Rules vs. Guardrails — The Most Important Lesson

This distinction was learned the hard way, through real bugs while building a working agent:

| | Prompt-based Rules | Code-based Guardrails |
|---|---|---|
| **What it is** | Plain English instructions in the system prompt | Actual Python logic that inspects input/output |
| **Enforcement** | Soft — a *request* the model tries to follow | Hard — the model cannot talk its way around it |
| **Failure mode** | Breaks silently on unanticipated phrasing, or when the model rationalizes a violation as "being helpful" | Breaks only if the *logic itself* is wrong |
| **When it's enough** | Common, well-behaved cases | Anything where a violation genuinely matters |

### Real failure modes discovered while building this agent, in order:
1. **Scope leak** — no rule against off-topic questions → agent answered "who's the US president?" (fix: explicit refusal rule)
2. **Hallucination** — model invented fake weather numbers by copying a text pattern it had seen earlier (fix: explicit "never invent numbers" rule)
3. **Fake data treated as real** — an extraction step meant to output *only* a city name instead answered a math question, and that answer got treated as a location (fix: `is_plausible_city()` — a real code check on format/length/characters)
4. **Stale/corrupted persistent memory** — a bad value saved *before* a guardrail existed kept getting reloaded and offered back, since the guardrail only protected new writes (fix: validate **on read**, not just on write — "self-healing" memory)
5. **Rationalized helpfulness** — asked to list cities directly, the model refused (rule caught it) — but reframed as "help me narrow down my location," it complied, because listing cities felt instrumentally helpful even though it was still off-topic (fix: a more specific rule closing that exact loophole)
6. **Phrasing-sensitive rule-following** — the *same* violation (listing cities) was refused when phrased "in multiple languages" but allowed when phrased "in only English" — proof the model was pattern-matching surface wording, not enforcing the rule's actual intent (fix: `looks_like_a_list()` — an **output-level guardrail** checking the shape of the reply itself, regardless of how the request was worded)

### The takeaway
No amount of rewording a prompt fully closes a loophole, because prompt rules are matched against *phrasing*, not *intent*. Real robustness comes from **defense in depth**:
- **Input shaping** — split "stated fact" from "inferred guess," confirm inferred ones
- **Candidate validation** — reject anything that doesn't structurally look like valid data, no matter what the model claims
- **Output validation** — inspect the model's final reply itself (format, structure, content) after generation, independent of how the request was phrased

---

## 🐛 Practical Debugging Lessons

- **"Did the code change but the behavior didn't?"** → First hypothesis: *am I actually running the file I think I'm running?* Stale cached downloads (e.g. `file (1).py`) are a common, easy-to-miss cause. Fix: print an explicit **version marker** at startup so there's never ambiguity about which build is running.
- **Syntax-valid ≠ structurally correct.** A file can compile cleanly (`py_compile` passes) while still being broken — e.g. an edit that accidentally deletes a function's `def` line leaves its body dangling as unreachable code, only surfacing as a `NameError` at runtime. Checking that all expected function names actually exist (via `ast.walk`) catches this; syntax checks alone don't.
- **A fix only protects data going forward.** Adding a guardrail doesn't retroactively clean up bad data already saved before the fix existed — that requires an explicit **validate-on-read / self-heal** step, not just validate-on-write.

---

## 🧠 The One Idea to Remember Above All

**The AI is stateless and "hand-less."** It can only generate text — it cannot remember, act, or enforce anything on its own. Every capability that makes an agent feel intelligent and reliable — memory, tool use, staying in scope, avoiding hallucination — is really built by **code and systems around the AI**, interpreting its output, feeding information back in, and validating what comes out before it reaches the user.

---

## 🖼️ Anchoring Analogies (Quick Recall)

- **Pizza shop employee** — brain = person, rules = manual, tools = register/phone, memory = recognizing regulars
- **Waiter & kitchen** — the AI requests, an outside system executes
- **Toolbox handyman** — picks a tool by matching its label to the job, not by inspecting internals
- **Doctor with no memory** — reads a fresh note before each patient; looks like they remember, but don't
- **Chef following a recipe** — think → act → check → repeat (the ReAct loop)
- **GPS with a retry limit** — stops looping after too many recalculations
- **Restaurant kitchen team** — specialized agents + a head chef as orchestrator
- **A rule vs. a locked door** — a prompt rule is a sign asking you not to enter; a code guardrail is a locked door that doesn't care how politely you ask