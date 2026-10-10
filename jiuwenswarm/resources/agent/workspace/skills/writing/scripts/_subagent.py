"""Typed agent invocation for the evidence-first writing workflow."""
from __future__ import annotations
import asyncio, json, os, re
from pathlib import Path
from openjiuwen.agent_teams.workflow.engine.facade import agent
REFERENCES_DIR=Path(__file__).resolve().parent.parent/'references'
_SECTION_WRITERS = {'method_writer', 'results_writer', 'related_work_writer',
                    'introduction_writer', 'limitations_writer', 'conclusion_writer'}
_FILES={
 'formatter':'formatter.md',
 'visual_planner':'visual_planner.md','visual_reviewer':'visual_reviewer.md',
 'paper_architect':'paper_architect.md','method_writer':'method_writer.md','results_writer':'results_writer.md',
 'related_work_writer':'related_work_writer.md','introduction_writer':'introduction_writer.md',
 'limitations_writer':'limitations_writer.md','conclusion_writer':'conclusion_writer.md','integration_editor':'integration_editor.md',
 'claim_verifier':'claim_verifier.md','literature_novelty_reviewer':'literature_novelty_reviewer.md',
 'argument_reviewer':'argument_reviewer.md','revision_coordinator':'revision_coordinator.md',
 'revision_adjudicator':'revision_adjudicator.md',
 'final_paper_reviewer':'final_paper_reviewer.md',
}
_PROTOCOL_VERSION = "writing-v3.0"
_JSON_RETRY_NONCE = os.urandom(8).hex()


class AgentCallError(RuntimeError):
 """A recoverable, role-labelled LLM invocation failure."""

 def __init__(self, name: str, reason: str) -> None:
  self.name=name
  self.reason=reason
  super().__init__(f"{name}: {reason}")


def extract_persona(path: Path)->str:
 text=path.read_text(encoding='utf-8'); match=re.search(r'##\s+Inline Persona for Teammate\s*\n```\n?(.*?)```',text,re.S)
 if not match: raise RuntimeError(f'missing persona: {path}')
 return match.group(1).strip()


_STABLE_PROMPT_KEYS = (
 'paper_contract', 'chapter_contract', 'evidence', 'asset_manifest',
 'contract_conflicts', 'section_id', 'shared_handoffs', 'handoffs', 'sections',
)


def _canonical_prompt_payload(payload: dict) -> dict:
 """Keep immutable evidence first and nested mappings byte-stable.

 Provider prompt caches are prefix based.  Scientific meaning does not depend
 on JSON member order, so put stable contracts/evidence before changing drafts
 and canonicalise nested mappings.  This improves prefix reuse without ever
 reusing an inexact response.
 """
 def canonical(value):
  if isinstance(value, dict):
   return {key: canonical(value[key]) for key in sorted(value)}
  if isinstance(value, list):
   return [canonical(item) for item in value]
  return value
 ordered = {}
 for key in _STABLE_PROMPT_KEYS:
  if key in payload:
   ordered[key] = canonical(payload[key])
 for key in sorted(set(payload) - set(ordered)):
  ordered[key] = canonical(payload[key])
 return ordered


def _normalise_handoff_conflicts(handoff: dict, *, owner: str) -> None:
 """Move prose notes out of the blocking conflict channel.

 A bare string cannot be routed or resolved.  Treat it as an open dependency;
 only complete machine-routable contradictions remain hard conflicts.
 """
 raw = handoff.get('conflicts')
 if not isinstance(raw, list):
  handoff['conflicts'] = []
  return
 required = ('conflict_id', 'owner', 'required_action', 'next_action')
 valid, notes = [], []
 for item in raw:
  if isinstance(item, dict) and all(str(item.get(field) or '').strip() for field in required) and str(item.get('statement') or item.get('message') or '').strip():
   valid.append(item)
  else:
   if isinstance(item, dict):
    note = str(item.get('statement') or item.get('message') or '').strip()
   else:
    note = str(item or '').strip()
   if note:
    notes.append(note)
 handoff['conflicts'] = valid
 dependencies = handoff.get('open_dependencies')
 if not isinstance(dependencies, list):
  dependencies = []
 handoff['open_dependencies'] = list(dict.fromkeys(
  [str(item) for item in dependencies if str(item).strip()] + notes
 ))


def _section_quality_instruction(payload: dict) -> str:
 """Make prose-length requirements concrete for every section writer.

 LLMs commonly interpret a lower bound as a target and estimate word counts
 loosely.  The contract has deterministic limits, so give the model the
 current section's explicit generation floor and any measured defects before
 it writes.  This improves first-pass compliance without changing evidence.
 """
 contract = payload.get('chapter_contract') if isinstance(payload.get('chapter_contract'), dict) else {}
 if not contract:
  return ''
 quality = contract.get('quality_requirements') if isinstance(contract.get('quality_requirements'), dict) else {}
 targets = contract.get('drafting_targets') if isinstance(contract.get('drafting_targets'), dict) else {}
 min_paragraphs = int(quality.get('min_paragraphs') or 0)
 min_words = int(quality.get('min_words') or 0)
 min_each = int(quality.get('min_words_per_paragraph') or 0)
 target_total = int(targets.get('target_words') or min_words)
 target_each = int(targets.get('target_words_per_paragraph') or min_each)
 defects = payload.get('quality_errors') if isinstance(payload.get('quality_errors'), list) else []
 text = (
  '\n\nLENGTH AND COVERAGE CONTROL (mandatory): Count English words in each '
  'paragraph before returning JSON. The deterministic lower bounds are '
  f'{min_paragraphs} paragraphs, {min_words} total words, and {min_each} words '
  f'per paragraph. Generate to the safer floors of {target_total} total words '
  f'and {target_each} words per paragraph; do not aim at the lower bound. '
  'Citations, Figure/Table markers, IDs, and punctuation are not a substitute '
  'for substantive prose. Use supplied facts only, but explain their role, '
  'scope, assumptions, and limitations rather than padding or repeating text.'
 )
 if defects:
  text += (' Repair every listed measured defect below. Return complete replacement '
           'paragraph objects whose text independently meets the safer floor; '
           'do not merely append a short sentence. DEFECTS: ' + json.dumps(defects, ensure_ascii=False))
 return text


def _parse_json_object(text: object) -> dict | None:
 """Recover one JSON object from common model formatting defects."""
 raw = str(text or '').strip()
 candidates = [raw]
 fenced = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw, flags=re.I | re.S).strip()
 if fenced != raw:
  candidates.append(fenced)
 start = raw.find('{')
 if start >= 0:
  candidates.append(raw[start:])
 for candidate in candidates:
  try:
   value = json.loads(candidate)
  except (ValueError, TypeError):
   try:
    value, _ = json.JSONDecoder().raw_decode(candidate)
   except (ValueError, TypeError):
    continue
  if isinstance(value, dict):
   return value
 try:
  from json_repair import repair_json
  repaired = repair_json(raw, return_objects=True)
  return repaired if isinstance(repaired, dict) else None
 except Exception:
  return None


def _has_balanced_json_delimiters(text: object) -> bool:
 """Check brace/bracket balance without being confused by quoted strings.

 A permissive JSON repair library is useful for harmless formatting noise, but
 accepting an object reconstructed from an API-truncated completion hides the
 actual failure and pushes it into a much more expensive writing/review loop.
 """
 raw = str(text or "")
 stack: list[str] = []
 in_string = False
 escaped = False
 pairs = {"}": "{", "]": "["}
 for character in raw:
  if in_string:
   if escaped:
    escaped = False
   elif character == "\\":
    escaped = True
   elif character == '"':
    in_string = False
   continue
  if character == '"':
   in_string = True
  elif character in "{[":
   stack.append(character)
  elif character in pairs:
   if not stack or stack.pop() != pairs[character]:
    return False
 return not in_string and not stack


async def call_agent_async(name:str,payload:dict,*,phase:str|None=None)->dict:
 if name not in _FILES: raise ValueError(name)
 if _is_mock_runtime(): return _mock(name,payload)
 prompt_payload = _canonical_prompt_payload(payload)
 prompt=(extract_persona(REFERENCES_DIR/_FILES[name])+
         f"\n\nPROTOCOL VERSION: {_PROTOCOL_VERSION}"+
         (_section_quality_instruction(payload) if name in _SECTION_WRITERS else '')+
         "\n\nINPUT JSON:\n"+json.dumps(prompt_payload,ensure_ascii=False,separators=(',', ':')))
 timeout_seconds=int(os.getenv('WRITING_AGENT_TIMEOUT_SECONDS','300'))
 configured_raw = os.getenv('WRITING_AGENT_MAX_ATTEMPTS')
 configured_attempts=max(1,int(configured_raw or '2'))
 # Section writers produce the longest structured objects.  Give only their
 # malformed/truncated responses one additional bounded recovery opportunity;
 # successful calls still cost exactly one request.
 # An explicit deployment cap remains authoritative.  The safer default is
 # three attempts only when the operator did not set WRITING_AGENT_MAX_ATTEMPTS.
 max_attempts=(3 if name in _SECTION_WRITERS and configured_raw is None else configured_attempts)
 last_reason = 'returned invalid JSON'
 for attempt in range(1,max_attempts+1):
  attempt_prompt = prompt
  if attempt > 1:
    attempt_prompt += (
     "\n\nRETRY CONTROL: The previous response was invalid or incomplete JSON. "
     "Return the required object only, with balanced braces and no Markdown. "
     "Keep every required field, but omit repeated explanations and optional examples before shortening required text. "
     f"retry={attempt}; nonce={_JSON_RETRY_NONCE}"
    )
  try:
   text=await asyncio.wait_for(agent(attempt_prompt,label=name,phase=phase,schema=None),timeout=timeout_seconds)
  except TimeoutError as exc:
   last_reason=f"timed out after {timeout_seconds}s"
   if attempt == max_attempts:
    raise AgentCallError(name,f"timed out after {timeout_seconds}s on {max_attempts} attempt(s)") from exc
   await asyncio.sleep(min(2**(attempt-1),5))
   continue
  except Exception as exc:
   last_reason=f"backend call failed: {type(exc).__name__}"
   if attempt == max_attempts:
    raise AgentCallError(name,last_reason) from exc
   await asyncio.sleep(min(2**(attempt-1),5))
   continue
  if str(text).lstrip().startswith('[mock:'):
   return _mock(name,payload)
  # Do not let json_repair silently manufacture a plausible object from a
  # cut-off completion.  A fresh, compact retry is more truthful and avoids
  # later protocol failures that look like missing evidence assertions.
  if not _has_balanced_json_delimiters(text):
   last_reason = 'returned truncated JSON'
   continue
  value = _parse_json_object(text)
  if value is not None:
   return _repair_response(name,payload,value)
  last_reason = 'returned invalid JSON'
 if last_reason == 'returned invalid JSON':
  raise AgentCallError(name,f"returned invalid JSON on {max_attempts} attempt(s)")
 raise AgentCallError(name,last_reason)
def _is_mock_runtime()->bool:
 # MockBackend has no environment marker; its response format is handled by
 # the direct fallback below only when it is received. Normal calls stay real.
 return False


def _ensure_asset_references(section, contract: dict) -> None:
 """Add an explicit Figure/Table reference for every declared paragraph asset."""
 if not isinstance(section, dict):
  return
 kinds = {
  str(item.get('id')): str(item.get('kind') or '')
  for item in contract.get('allowed_assets') or []
  if isinstance(item, dict) and str(item.get('id') or '').strip()
 }
 if not kinds:
  return
 for paragraph in section.get('paragraphs') or []:
  if not isinstance(paragraph, dict):
   continue
  text = str(paragraph.get('text') or '')
  missing = []
  for asset_id in paragraph.get('asset_ids') or []:
   asset_id = str(asset_id)
   prefix = 'Figure' if kinds.get(asset_id) == 'figure' else 'Table'
   if asset_id in kinds and f'{prefix} [{asset_id}]' not in text:
    missing.append(f'{prefix} [{asset_id}]')
  if missing:
   paragraph['text'] = text.rstrip() + ' See ' + ' and '.join(missing) + '.'


def _reconcile_asset_references(section, handoff, contract: dict) -> None:
 """Drop stale visual references after a chapter contract is refreshed.

 An asset revision may replace or retire a visual.  If a writer then carries
 an old ``asset_ids`` value into the new contract version, the reference is
 invalid by construction; retaining it turns a safe version transition into a
 fatal protocol error.  Only invalid Figure/Table markers and identifiers are
 removed here—no evidence assertion or scientific wording is invented.
 """
 if not isinstance(section, dict):
  return
 allowed = {
  str(item.get('id')) for item in contract.get('allowed_assets') or []
  if isinstance(item, dict) and str(item.get('id') or '').strip()
 }
 removed: set[str] = set()
 for paragraph in section.get('paragraphs') or []:
  if not isinstance(paragraph, dict):
   continue
  raw_ids = paragraph.get('asset_ids') if isinstance(paragraph.get('asset_ids'), list) else []
  invalid = {str(asset_id) for asset_id in raw_ids if str(asset_id) not in allowed}
  if not invalid:
   continue
  removed.update(invalid)
  paragraph['asset_ids'] = [str(asset_id) for asset_id in raw_ids if str(asset_id) in allowed]
  text = str(paragraph.get('text') or '')
  for asset_id in invalid:
   text = re.sub(r'\s*(?:Figure|Table)\s*\[' + re.escape(asset_id) + r'\]', '', text, flags=re.I)
  paragraph['text'] = re.sub(r'\s{2,}', ' ', text).strip()
 if removed and isinstance(handoff, dict) and isinstance(handoff.get('used_asset_ids'), list):
  handoff['used_asset_ids'] = [
   str(asset_id) for asset_id in handoff['used_asset_ids'] if str(asset_id) in allowed
  ]


def _normalise_assertion_methods(section, contract: dict) -> None:
 """Expand only unambiguous method aliases to their canonical contract name."""
 if not isinstance(section, dict):
  return
 scopes = contract.get('experiment_scopes') if isinstance(contract.get('experiment_scopes'), dict) else {}
 for paragraph in section.get('paragraphs') or []:
  if not isinstance(paragraph, dict):
   continue
  for assertion in paragraph.get('evidence_assertions') or []:
   if not isinstance(assertion, dict):
    continue
   scope = scopes.get(str(assertion.get('experiment_id') or ''))
   methods = [str(item) for item in (scope or {}).get('methods') or []] if isinstance(scope, dict) else []
   if not methods:
    continue
   for field in ('method', 'comparison_method'):
    value = str(assertion.get(field) or '').strip()
    if not value or value in methods:
     continue
    if len(value) < 3:
     continue
    hits = [method for method in methods
            if method.casefold().startswith(value.casefold()) or value.casefold() in method.casefold()]
    if len(hits) == 1:
     assertion[field] = hits[0]


def _repair_response(name: str, payload: dict, value: dict) -> dict:
 """Recover schema containers solely from already validated input payloads."""
 if not isinstance(value, dict):
  return value
 if name in _SECTION_WRITERS:
  contract = payload.get('chapter_contract') if isinstance(payload.get('chapter_contract'), dict) else {}
  _normalise_assertion_methods(value.get('section'), contract)
  _reconcile_asset_references(value.get('section'), value.get('chapter_handoff'), contract)
  _ensure_asset_references(value.get('section'), contract)
  if not isinstance(value.get('chapter_handoff'), dict):
   paragraphs = (value.get('section') or {}).get('paragraphs') or [] if isinstance(value.get('section'), dict) else []
   used_claim_ids = list(dict.fromkeys(
    str(item) for paragraph in paragraphs if isinstance(paragraph, dict)
    for item in paragraph.get('claim_ids') or []
   ))
   used_asset_ids = list(dict.fromkeys(
    str(item) for paragraph in paragraphs if isinstance(paragraph, dict)
    for item in paragraph.get('asset_ids') or []
   ))
   value['chapter_handoff'] = {
    'contract_version': contract.get('contract_version'),
    'used_claim_ids': used_claim_ids, 'used_asset_ids': used_asset_ids,
    'defined_terms': [], 'open_dependencies': [], 'conflicts': [],
   }
  _normalise_handoff_conflicts(value['chapter_handoff'], owner=name)
 if name == 'integration_editor':
  supplied = payload.get('sections') if isinstance(payload.get('sections'), dict) else {}
  returned = value.get('sections') if isinstance(value.get('sections'), dict) else {}
  updates = value.get('section_updates') if isinstance(value.get('section_updates'), dict) else {}
  merged = {section_id: section for section_id, section in supplied.items()}
  for section_id, draft in supplied.items():
   edited = returned.get(section_id)
   compact = updates.get(section_id)
   if not isinstance(draft, dict):
    continue
   new_text = {}
   if isinstance(edited, dict):
    new_text.update({
     str(item.get('paragraph_id')): str(item.get('text'))
     for item in edited.get('paragraphs') or []
     if isinstance(item, dict) and str(item.get('paragraph_id') or '').strip()
     and str(item.get('text') or '').strip()
    })
   if isinstance(compact, dict) and isinstance(compact.get('paragraph_text'), dict):
    new_text.update({
     str(paragraph_id): str(text)
     for paragraph_id, text in compact['paragraph_text'].items()
     if str(paragraph_id or '').strip() and str(text or '').strip()
    })
   section = dict(draft)
   if isinstance(edited, dict) and str(edited.get('title') or '').strip():
    section['title'] = str(edited['title'])
   elif isinstance(compact, dict) and str(compact.get('title') or '').strip():
    section['title'] = str(compact['title'])
   paragraphs = []
   paper_contract = payload.get('paper_contract') if isinstance(payload.get('paper_contract'), dict) else {}
   section_requirements = (paper_contract.get('section_quality_requirements') or {}).get(section_id) or {}
   min_paragraph_words = int(section_requirements.get('min_words_per_paragraph') or 0)
   for item in draft.get('paragraphs') or []:
    if not isinstance(item, dict):
     paragraphs.append(item)
     continue
    paragraph_id = str(item.get('paragraph_id') or '')
    candidate = new_text.get(paragraph_id)
    # Integration may make prose more concise, but cannot replace an argument
    # unit with a fragment below that section's publication lower bound.
    if candidate and len(candidate.split()) >= min_paragraph_words:
     original_text = str(item.get('text') or '')
     protected_tokens = re.findall(r'(?:Figure|Table) \[[^\]]+\]|\[cite:[^\]]+\]', original_text)
     missing_tokens = [token for token in protected_tokens if token not in candidate]
     if missing_tokens:
      candidate = candidate.rstrip() + ' ' + ' '.join(missing_tokens)
     item = {**item, 'text': candidate}
    paragraphs.append(item)
   section['paragraphs'] = paragraphs
   merged[section_id] = section
  value['sections'] = merged
  value.pop('section_updates', None)
  if not isinstance(value.get('integration_handoff'), dict):
   contract = payload.get('paper_contract') if isinstance(payload.get('paper_contract'), dict) else {}
   value['integration_handoff'] = {'contract_version': contract.get('contract_version'), 'conflicts': []}
  _normalise_handoff_conflicts(value['integration_handoff'], owner=name)
 return value


def _mock_section(section_id:str,payload:dict)->dict:
 contract=payload.get('chapter_contract',{}) if isinstance(payload.get('chapter_contract'),dict) else {}
 paper_contract=payload.get('paper_contract',{}) if isinstance(payload.get('paper_contract'),dict) else {}
 quality=contract.get('quality_requirements',{}) if isinstance(contract.get('quality_requirements'),dict) else {}
 roles=quality.get('required_roles') or ['section']
 citations=contract.get('allowed_citation_ids') or []
 claim_ids=contract.get('allowed_claim_ids') or []
 min_assertions=int(quality.get('min_evidence_assertions') or 0)
 aggregate=next((item for item in paper_contract.get('aggregates') or []
                 if isinstance(item,dict) and item.get('experiment_id') and item.get('method')
                 and item.get('metric') and isinstance(item.get('mean'),(int,float))),None)
 mock_assertion=({'type':'metric_value','aggregate_id':aggregate['id'],
                   'experiment_id':aggregate['experiment_id'],'method':aggregate['method'],
                   'metric':aggregate['metric'],'statistic':'mean',
                   'run_count':aggregate.get('run_count'),'value':aggregate['mean']}
                 if aggregate else None)
 assets=[item for item in contract.get('allowed_assets') or [] if isinstance(item,dict) and item.get('id')]
 paragraphs=[]
 for index,role in enumerate(roles):
  citation=f" [cite:{citations[index % len(citations)]}]" if citations else ''
  body=('This synthetic workflow validation paragraph explains the bounded research setting, the relevant design '
        'decision, its relation to the research question, and the limits imposed by the supplied evidence. ' * 8).strip()
  assigned_assets=[assets[index % len(assets)]] if assets and section_id=='experiments' else []
  asset_text=''.join(f" {'Figure' if item.get('kind')=='figure' else 'Table'} [{item['id']}] is included as a dry-run structural reference." for item in assigned_assets)
  assertions=([dict(mock_assertion)] if mock_assertion and section_id in {'experiments','conclusion'} else [])
  if index == 0 and assertions:
   assertions *= max(1,min_assertions)
  paragraphs.append({'paragraph_id':f'{section_id}-p{index+1}','rhetorical_role':role,'text':body+citation+asset_text,
                     'claim_ids':claim_ids,'asset_ids':[str(item['id']) for item in assigned_assets],
                     'evidence_assertions':assertions})
 return {'section':{'section_id':section_id,'title':section_id.replace('_',' ').title(),'paragraphs':paragraphs},
         'chapter_handoff':{'contract_version':contract.get('contract_version'),'used_claim_ids':claim_ids,
                            'used_asset_ids':[],'defined_terms':[],'open_dependencies':[],'conflicts':[]}}


def _mock(name:str,payload:dict)->dict:
 section_id=str(payload.get('section_id','section'))
 if name=='paper_architect':
  contract=payload.get('paper_contract',{}) if isinstance(payload.get('paper_contract'),dict) else {}
  evaluated_claim_ids=[str(item.get('id')) for item in contract.get('claims') or []
                       if isinstance(item,dict) and item.get('id')
                       and (str(item.get('evaluation_status') or '')=='evaluated'
                            or str(item.get('status') or '') in {'supported','not_supported','inconclusive'})]
  return {'argument_map':{'chapter_plans':{section_id:{'purpose':f'Write {section_id}.',
          'claim_ids':evaluated_claim_ids if section_id in {'experiments','conclusion'} else [],
          'dependencies':[],'prohibitions':['Do not create evidence outside the paper contract.']}
          for section_id in ('method','experiments','related_work','introduction','limitations','conclusion')}}}
 if name in {'method_writer','results_writer','related_work_writer','introduction_writer','limitations_writer','conclusion_writer'}:
  return _mock_section(section_id,payload)
 if name=='integration_editor': return {'sections':payload.get('sections',{}),'integration_handoff':{'contract_version':payload.get('paper_contract',{}).get('contract_version'),'conflicts':[]}}
 if name in {'claim_verifier','literature_novelty_reviewer','argument_reviewer','final_paper_reviewer'}: return {'verdict':'pass','issues':[]}
 if name=='revision_coordinator': return {'verdict':'pass','actions':[],'conflicts':[]}
 if name=='revision_adjudicator': return {'verdict':'repair','repairs':[]}
 if name=='visual_planner': return {'assets':[],'rationale':'Mock runtime delegates to the deterministic visual-plan fallback.'}
 if name=='visual_reviewer': return {'verdict':'pass','issues':[]}
 abstract=(
  'This synthetic dry-run abstract validates the manuscript orchestration contract without making a scientific '
  'claim or substituting generated values for verified experimental evidence. It exercises the required title, '
  'abstract, section, review, revision, asset, citation, and export interfaces using clearly labelled placeholder '
  'language. The dry run checks that every stage can exchange structured JSON, preserve immutable identifiers, '
  'retain evidence metadata, and terminate under bounded retry and no-progress rules. It also checks that chapter '
  'quality constraints are evaluated before integration, that compact paragraph updates retain protected fields, '
  'and that malformed handoff notes do not become unresolved scientific conflicts. Visual assets remain derived '
  'from the supplied upstream records, while this abstract deliberately avoids interpreting their numerical '
  'meaning. Review agents return empty issue lists only to test the successful control-flow branch; this behavior '
  'does not certify the scientific validity, novelty, statistical adequacy, or publication readiness of a real '
  'manuscript. The generated document is therefore a software smoke-test artifact rather than a research result. '
  'Its sole conclusion is that the deterministic plumbing can carry a contract-compliant payload through the '
  'writing pipeline without an unbounded loop, schema loss, fabricated citation, or accidental mutation of '
  'upstream evidence. A real run must still use the configured language model, verified literature, genuine '
  'experimental outputs, and all final human or automated scientific gates required by the project.'
 )
 return {'title':'Anonymous Research Submission','abstract':abstract,'claim_ids':[]}
