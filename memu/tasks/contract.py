"""Prompt assembly, bounded reading and strict task-result validation.

Providers return proposals. This module never writes a repository file.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Callable

from pydantic import ValidationError

from ..config import Config
from ..refs import Ref, merge_refs
from ..types import (EntriesOutput, SummaryOutput, UnitsOutput, RelationsOutput,
                     CompactEntriesOutput, CompactUnitsOutput, TopicsOutput,
                     WorklogEntriesOutput, WorklogUnitsOutput)
from ..task_material import model_material
from ..compaction import evidence_refs, normalize_cited_coverage, validate_coverage
from ..events import event
from ..models import ModelOutputError, OutputTruncated


class TaskValidationError(ValueError):
    code = 'task_validation_failed'

    def __init__(self, message):
        super().__init__(message)
        # Categorize our fixed validation rules, never emit arbitrary exception text.
        rules = {
            'Worklog anchor': 'worklog_anchor_not_cited',
            'Choose an actual': 'worklog_anchor_not_observation',
            'Worklog attribution': 'worklog_role_mismatch',
            'Compact memory': 'context_budget',
            'Compact coverage': 'coverage_invalid',
            'New compact output': 'coverage_citation',
            'Reference outside': 'evidence_scope',
            'Reference does not exist': 'evidence_missing',
            'View evidence': 'view_evidence_type',
            'Summary repeats': 'summary_duplicate_matter',
            'Summary coverage': 'summary_coverage',
            'Summary exceeds its entry count': 'summary_entry_count',
            'Summary exceeds its matter-level': 'summary_matter_budget',
            'Summary exceeds its abstraction-level': 'summary_abstraction_budget',
            'MEMORY overview exceeds': 'memory_budget',
            'Runtime overview exceeds': 'runtime_budget',
            'Task memory_read call': 'read_call_budget',
            'Task memory_read output': 'read_character_budget',
            'Conversation output exceeds its entry': 'conversation_entry_budget',
            'Conversation output exceeds its body': 'conversation_body_budget',
        }
        self.reason = next((code for prefix, code in rules.items() if str(message).startswith(prefix)),
                           'output_contract')


@dataclass(frozen=True)
class TaskDefinition:
    name: str
    output_type: type
    prompt_version: str


PROMPTS = Path(__file__).with_name("prompts")
TASKS = {
    "digest": TaskDefinition("digest", EntriesOutput, "v1.3"),
    "unit": TaskDefinition("unit", UnitsOutput, "v1.3"),
    "relate": TaskDefinition("relate", RelationsOutput, "v1.3"),
    "rollup": TaskDefinition("rollup", EntriesOutput, "v1.6"),
    "topics": TaskDefinition("topics", TopicsOutput, "v1.4"),
}


class ScopedReader:
    def __init__(self, allowed_refs, read_ref, max_calls, max_chars):
        self.allowed = [Ref.parse(ref) for ref in allowed_refs]
        self.read_ref = read_ref
        self.max_calls = max_calls
        self.max_chars = max_chars
        self.calls = 0
        self.chars = 0

    def check_scope(self, value: str) -> Ref:
        ref = Ref.parse(value)
        if not any(allowed.contains(ref) for allowed in self.allowed):
            raise TaskValidationError(f"Reference outside this task's material: {value}")
        return ref

    def verify(self, value: str):
        ref = self.check_scope(value)
        if self.read_ref is None:
            raise TaskValidationError("Repository reference validator is required")
        result = self.read_ref(ref.canonical)
        if result is None or result is False:
            raise TaskValidationError(f"Reference does not exist: {value}")
        if isinstance(result, dict) and (result.get("deleted") or result.get("withheld")):
            raise TaskValidationError(f"Reference is unavailable: {value}")
        return result

    def __call__(self, value: str):
        if self.calls >= self.max_calls:
            raise TaskValidationError("Task memory_read call budget exhausted")
        result = self.verify(value)
        size = len(json.dumps(result, ensure_ascii=False, default=str))
        if self.chars + size > self.max_chars:
            raise TaskValidationError("Task memory_read output budget exhausted")
        self.calls += 1
        self.chars += size
        return result


class TaskRunner:
    def __init__(self, model, config: Config):
        self.model = model
        self.config = config

    def _rollup_scope(self, scope):
        """Use the same prose budgets for prompting and post-generation checks."""
        scope = deepcopy(scope)
        view = scope.get('view')
        total = {'day': self.config.views.day_summary_chars,
                 'month': self.config.views.month_summary_chars,
                 'runtime': self.config.views.per_topic_chars,
                 'memory': self.config.views.memory_overview_chars}.get(view)
        if total is None:
            return scope
        entry = min({'day': 500, 'month': 100}.get(view, total), total)
        scope['entry_chars'] = min(scope.get('entry_chars', entry), entry)
        scope['total_chars'] = total
        if view == 'day':
            scope['entry_target_chars'] = min(160, scope['entry_chars'])
        if view in {'day', 'month'}:
            # A batch of Units is evidence, not a count of independent matters.
            # Plan entry count using the writing target, not the larger safety cap.
            count = max(1, total // scope.get('entry_target_chars', scope['entry_chars']))
            scope['max_entries'] = min(scope.get('max_entries', count), count)
        return scope

    def prepare(self, task: str, material: Any, scope: dict, *, mode="build") -> dict:
        if task not in TASKS:
            raise TaskValidationError(f"Unknown task: {task}")
        if mode not in {"build", "update", "rebuild"}:
            raise TaskValidationError(f"Unknown task mode: {mode}")
        if task == 'rollup':
            if 'view' not in scope and isinstance(material, dict) and 'view' in material:
                scope = {**scope, 'view': material['view']}
            scope = self._rollup_scope(scope)
        material = model_material(task, material)
        definition = TASKS[task]
        common = (PROMPTS / "common.md").read_text(encoding="utf-8")
        prompt = (PROMPTS / f"{task}.md").read_text(encoding="utf-8")
        compact = task in {'digest', 'unit'} and isinstance(material, dict) and 'context' in material
        output_type = (CompactEntriesOutput if task == 'digest' else CompactUnitsOutput) if compact else definition.output_type
        if task == 'rollup' and scope.get('view') in {'day', 'month'}:
            output_type = SummaryOutput
        worklog = compact and material.get('evidence_contract') == 'local-ai-v1'
        if worklog:
            output_type = WorklogEntriesOutput if task == 'digest' else WorklogUnitsOutput
        if compact:
            prompt += '\n\n' + (PROMPTS / 'compact.md').read_text(encoding='utf-8')
        if worklog:
            prompt += '\n\n' + (PROMPTS / 'worklog.md').read_text(encoding='utf-8')
        if compact and material.get('unit_limit'):
            prompt += ('\n\n本次是有上限的对话片段。围绕用户目标、决定、实际结果和未决问题输出完整主结果，'
                       '不逐条列日志、工具操作或环境侦察。片段归并时保留提供的各条事实与限定。'
                       'unit_limit 是整段允许的最大 Unit 数；digest 每批最多三条短摘要。')
        output_schema = output_type.model_json_schema()
        if compact and material.get('unit_limit'):
            field = 'entries' if task == 'digest' else 'units'
            output_schema['properties'][field]['maxItems'] = 3 if task == 'digest' else material['unit_limit']
            entry_name = ('Worklog' if worklog else '') + ('Entry' if task == 'digest' else 'UnitDraft')
            output_schema['$defs'][entry_name]['properties']['body']['maxLength'] = 350 if task == 'digest' else 800
        if task in {"unit", "digest"} and isinstance(material, dict) and material.get("items"):
            # Filtering happens before task dispatch. Never acknowledge a
            # nonempty source as remembered when the model silently drops it.
            output_schema["properties"]["units" if task == "unit" else "entries"]["minItems"] = (
                0 if compact and (material['context']['recorded'] or
                                  material.get('material_kind') == 'agent_worklog') else 1)
        if task == "digest" and isinstance(material, dict) and material.get("items") and all(
                isinstance(item, dict) and "source_slice" in item for item in material["items"]):
            # The larger offset origin is not evidence admitted to this chunk.
            # Constrain generation as well as validating the model's references.
            output_schema["$defs"]['WorklogEntry' if worklog else 'Entry']["properties"]["facts"]["items"]["enum"] = list(dict.fromkeys(
                item["ref"] for item in material["items"]))
        if compact:
            refs = evidence_refs(material['items'], material['context'])
            entry_type = 'Entry' if task == 'digest' else 'UnitDraft'
            if worklog:
                entry_type = 'Worklog' + entry_type
                anchors = [item['ref'] for item in material['items']
                           if item.get('provenance', {}).get('event_kind') in
                           {'message', 'user_answer', 'tool_result', 'notification'}]
                if anchors:
                    output_schema['$defs'][entry_type]['properties']['anchor']['enum'] = list(dict.fromkeys(anchors))
                else:
                    output_schema['properties']['entries' if task == 'digest' else 'units']['maxItems'] = 0
            output_schema['$defs'][entry_type]['properties']['facts']['items']['enum'] = refs
            memory = output_schema['$defs']['ContextMemory']['properties']
            memory['summary']['maxLength'] = max(1, material['context']['memory_chars'] // 2)
            memory['facts']['items']['enum'] = refs
            memory['facts']['maxItems'] = max(1, min(16, material['context']['memory_chars'] // 40))
            if not worklog:
                coverage = output_schema['$defs']['MaterialCoverage']['properties']
                coverage['item']['maximum'] = len(material['items']) - 1
                ids = [record['id'] for record in material['context']['recorded']]
                if ids:
                    coverage['previous']['items']['enum'] = ids
                else:
                    coverage['previous']['maxItems'] = 0
                output_schema['properties']['coverage']['minItems'] = len(material['items'])
                output_schema['properties']['coverage']['maxItems'] = len(material['items'])
        if task == 'rollup' and scope.get('refs'):
            # Runtime records contain nested Unit citations, but MEMORY's next
            # hop must be one of the entries actually admitted to this task.
            # Constrain generation, not only the post-generation scope check.
            output_schema['$defs']['Entry']['properties']['facts']['items']['enum'] = list(dict.fromkeys(
                Ref.parse(ref).canonical for ref in scope['refs']))
        if task == 'rollup':
            # A decoding grammar can close a maxLength string mid-word. Keep
            # evidence/JSON constraints strict, but repair overlong prose after
            # generation instead of forcing a syntactically valid fragment.
            if scope.get('max_entries'):
                output_schema['properties']['entries']['maxItems'] = scope['max_entries']
            if output_type is SummaryOutput:
                output_schema['properties']['agenda']['maxItems'] = scope['max_entries']
                output_schema['$defs']['SummaryMatter']['properties']['refs']['items']['enum'] = scope.get('refs', [])
        if task == 'topics':
            output_schema['$defs']['TopicProposal']['properties']['facts']['items']['enum'] = scope['refs']
        if task == 'unit':
            topic_schema = output_schema['$defs']['WorklogUnitDraft' if worklog else 'UnitDraft']['properties']['topics']
            known_topics = [topic['key'] for topic in material.get('topics', [])]
            if known_topics:
                topic_schema['items']['enum'] = known_topics
            else:
                topic_schema['maxItems'] = 0
        payload = {
            "task": task, "mode": mode, "scope": deepcopy(scope),
            "material": deepcopy(material), "policy": self.config.task_policy(),
            "output_schema": output_schema,
        }
        if task == 'rollup':
            payload['policy'].pop('detail', None)
        # Read mappings and vocabulary from material/config; prompts don't invent them.
        if isinstance(material, dict) and "names" in material:
            payload["policy"]["names"] = deepcopy(material["names"])
        return {
            **payload,
            "prompt_version": definition.prompt_version + ('+compact-v1' if compact else ''),
            "prompt_hash": hashlib.sha256((common + prompt).encode()).hexdigest(),
            "messages": [
                {"role": "system", "content": common},
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False, sort_keys=True)},
            ],
        }

    def run(self, task: str, material: Any, scope: dict, *, mode="build", allowed_refs=None,
            read_ref: Callable | None = None, candidates=None, on_rejected: Callable | None = None) -> dict:
        feedback = None
        started = time.monotonic()
        # All repair attempts share the original read budget and evidence scope.
        readers = []
        for attempt in range(self.config.models.mllm.repair_attempts + 1):
            try:
                result = self._attempt(task, material, scope, mode=mode, allowed_refs=allowed_refs,
                                       read_ref=read_ref, candidates=candidates, feedback=feedback,
                                       readers=readers)
                event('model_task_done', task=task, attempt=attempt + 1,
                      elapsed_ms=round((time.monotonic() - started) * 1000))
                return result
            except OutputTruncated:
                raise  # The durable batch planner owns splitting/checkpointing.
            except (TaskValidationError, ModelOutputError) as error:
                if on_rejected is not None and hasattr(error, 'proposal'):
                    on_rejected(error, attempt + 1)
                event('model_task_repair', task=task, attempt=attempt + 1, error_code=error.code,
                      validation_reason=getattr(error, 'reason', 'output_contract'))
                if attempt == self.config.models.mllm.repair_attempts:
                    raise
                feedback = {'validation_error': str(error)[:2000],
                            'previous_proposal': getattr(error, 'proposal', None),
                            'instruction': '修复上次提案，只输出完整合法 JSON。不得放宽证据范围或编造证据。'}
                if task == 'rollup':
                    feedback['instruction'] += ('先合并同一事项并合并证据，再删去过程细节，以完整句子重写。'
                                                '不得截断正文、词语或代码来凑字符上限。')

    def _attempt(self, task, material, scope, *, mode, allowed_refs, read_ref, candidates,
                 feedback, readers):
        request = self.prepare(task, material, scope, mode=mode)
        scope = request['scope']
        if feedback:
            request['messages'].append({'role': 'user', 'content': json.dumps(feedback, ensure_ascii=False)})
        compact = task in {'digest', 'unit'} and isinstance(material, dict) and 'context' in material
        reader = ScopedReader(allowed_refs or [], read_ref,
                              0 if compact else self.config.read.task_read_calls,
                              self.config.read.task_read_chars)
        if readers:
            reader = readers[0]
        else:
            readers.append(reader)
        candidate_refs = {Ref.parse(ref).canonical for ref in (candidates or [])}
        if task == "relate":
            reader.allowed.extend(Ref.parse(ref) for ref in candidate_refs)
        request["tools"] = [{
            "name": "memory_read", "description": "Read only the current task's allowed material",
            "input_schema": {"type": "object", "properties": {"ref": {"type": "string"}},
                             "required": ["ref"], "additionalProperties": False},
        }] if read_ref is not None and reader.max_calls else []
        while True:
            result = self.model.complete(deepcopy(request))
            if not isinstance(result, dict):
                raise TaskValidationError("Model output must be a JSON object")
            if "tool_calls" not in result:
                break
            if set(result) != {"tool_calls"} or not isinstance(result["tool_calls"], list) or not result["tool_calls"]:
                raise TaskValidationError("Invalid model tool request")
            for call in result["tool_calls"]:
                if not isinstance(call, dict) or set(call) != {"name", "arguments"} or call["name"] != "memory_read":
                    raise TaskValidationError("Unsupported model tool")
                args = call["arguments"]
                if not isinstance(args, dict) or set(args) != {"ref"}:
                    raise TaskValidationError("Invalid memory_read arguments")
                value = reader(args["ref"])
                request["messages"].append({"role": "user", "content": json.dumps(
                    {"memory_read_result": {"ref": args["ref"], "data": value}}, ensure_ascii=False, default=str)})
        try:
            has_prior = compact and (bool(material['context']['recorded']) or material.get('material_kind') == 'agent_worklog')
            if task == "unit" and isinstance(material, dict) and material.get("items") and result.get("units") == [] and not has_prior:
                raise TaskValidationError("Nonempty Unit material must not be silently discarded")
            if task == "digest" and isinstance(material, dict) and material.get("items") and result.get("entries") == [] and not has_prior:
                raise TaskValidationError("Nonempty digest material must not be silently discarded")
            output_type = ((CompactEntriesOutput if task == 'digest' else CompactUnitsOutput)
                           if compact else TASKS[task].output_type)
            if task == 'rollup' and scope.get('view') in {'day', 'month'}:
                output_type = SummaryOutput
            worklog = compact and material.get('evidence_contract') == 'local-ai-v1'
            if worklog:
                output_type = WorklogEntriesOutput if task == 'digest' else WorklogUnitsOutput
            output = output_type.model_validate(result).model_dump(by_alias=True)
            if output_type is SummaryOutput:
                self._summary_coverage(output.pop('agenda'), output['entries'], scope)
            if compact and material.get('unit_limit'):
                entries = output['entries' if task == 'digest' else 'units']
                if len(entries) > (3 if task == 'digest' else material['unit_limit']):
                    raise TaskValidationError('Conversation output exceeds its entry limit')
                if any(len(entry['body']) > (350 if task == 'digest' else 800) for entry in entries):
                    raise TaskValidationError('Conversation output exceeds its body length limit')
            if compact:
                if worklog:
                    for entry in output['entries' if task == 'digest' else 'units']:
                        self._worklog_entry(entry, reader, material['items'])
                    self._worklog_coverage(output, material)
                else:
                    normalize_cited_coverage(output, material)
                validate_coverage(output, material)
                for ref in output['memory']['facts']:
                    reader.verify(ref)
            if task == "relate":
                relations = output["relations"]
                if len(relations) > self.config.build.relation_limit:
                    raise TaskValidationError("Too many relations")
                seen = set()
                for relation in relations:
                    if relation["target"] not in candidate_refs:
                        raise TaskValidationError("Relation target is not a supplied candidate")
                    if relation["rel"] not in self.config.build.relation_vocab:
                        raise TaskValidationError("Relation word is not in configured vocabulary")
                    key = (relation["rel"], relation["target"])
                    if key in seen:
                        raise TaskValidationError("Duplicate relation")
                    seen.add(key)
                    reader.verify(relation["target"])
            elif task == 'topics':
                keys = set()
                for topic in output['topics']:
                    if topic['key'] in keys:
                        raise TaskValidationError('Duplicate topic proposal')
                    keys.add(topic['key'])
                    for ref in topic['facts']:
                        reader.verify(ref)
            else:
                entries = output["units"] if task == "unit" else output["entries"]
                for entry in entries:
                    media_evidence = []
                    for value in entry["facts"]:
                        ref = Ref.parse(value)
                        if task in {"unit", "digest"} and ref.kind not in {"c", "s", "a"}:
                            raise TaskValidationError("New event evidence must be Source, detail or native media")
                        if task == "digest" and ref.kind == "s":
                            raise TaskValidationError("Digest evidence must be original Source or media")
                        if task == "rollup":
                            view = scope.get("view", material.get("view") if isinstance(material, dict) else None)
                            if ref.kind != "u" and not (view == "memory" and ref.kind == "r"):
                                raise TaskValidationError("View evidence must reference Units (or Runtime for MEMORY)")
                        evidence = reader.verify(value)
                        if task == "unit" and ref.kind == "s" and isinstance(evidence, dict):
                            media_evidence.extend(Ref.parse(item) for item in evidence.get("facts", [])
                                                  if Ref.parse(item).kind == "a")
                    if media_evidence:
                        direct_media = [Ref.parse(item) for item in entry["facts"] if Ref.parse(item).kind == "a"]
                        for required in media_evidence:
                            if not any(selected.alias == required.alias and
                                       (selected.contains(required) or required.contains(selected))
                                       for selected in direct_media):
                                raise TaskValidationError("Media-backed Unit must cite native media directly, not only its caption")
                    entry["facts"] = merge_refs(entry["facts"])
                if task == "rollup":
                    view = scope.get("view", material.get("view") if isinstance(material, dict) else None)
                    if scope.get('max_entries') and len(entries) > scope['max_entries']:
                        raise TaskValidationError(
                            f"Summary exceeds its entry count: {len(entries)} > {scope['max_entries']}; "
                            "merge related matters and their evidence before rewriting.")
                    seen = set()
                    for number, entry in enumerate(entries, 1):
                        body = entry['body'].strip()
                        if body in seen:
                            raise TaskValidationError(
                                f'Summary repeats identical matter text at entry {number}; merge its evidence.')
                        seen.add(body)
                    total = sum(len(entry["body"]) for entry in entries)
                    if scope.get('entry_chars'):
                        overlong = [(i, len(entry['body'])) for i, entry in enumerate(entries, 1)
                                    if len(entry['body']) > scope['entry_chars']]
                        if overlong:
                            raise TaskValidationError(
                                f"Summary exceeds its matter-level character budget: entry/length {overlong}, "
                                f"limit {scope['entry_chars']}; total {total}/{scope['total_chars']}. "
                                "Rewrite complete sentences; do not truncate text.")
                    if scope.get('total_chars') and total > scope['total_chars']:
                        label = {'memory': 'MEMORY overview exceeds', 'runtime': 'Runtime overview exceeds'}.get(
                            view, 'Summary exceeds its abstraction-level')
                        raise TaskValidationError(
                            f"{label} character budget: {total} > {scope['total_chars']}; "
                            "merge related matters and omit process details, preserving complete sentences.")
        except (ValueError, TypeError, KeyError) as error:
            if isinstance(error, TaskValidationError):
                error.proposal = result
                error.scope = scope
                raise
            failure = TaskValidationError(str(error))
            if isinstance(error, ValidationError):
                fields = {str(part) for item in error.errors(include_input=False, include_context=False)
                          for part in item['loc'] if part in {'body', 'facts', 'anchor', 'time', 'claim', 'topics', 'coverage', 'memory'}}
                failure.reason = 'output_schema_' + '_'.join(sorted(fields)) if fields else 'output_schema'
            failure.proposal = result
            failure.scope = scope
            raise failure from error
        return output

    @staticmethod
    def _summary_coverage(agenda, entries, scope):
        """All input must be considered; only relevant evidence is cited in prose."""
        admitted = {Ref.parse(ref).canonical for ref in scope.get('refs', [])}
        assigned = [Ref.parse(ref).canonical for group in agenda for ref in group['refs']]
        if set(assigned) - admitted:
            raise TaskValidationError('Reference outside this task\'s material in summary agenda')
        missing = len(admitted - set(assigned))
        repeated = len(assigned) - len(set(assigned))
        if missing or repeated or len(agenda) != len(entries):
            raise TaskValidationError(
                f'Summary coverage incomplete: missing={missing}, repeated={repeated}, '
                f'groups={len(agenda)}, entries={len(entries)}. '
                'Group every scope.refs item exactly once, then write one entry per matter; '
                'do not summarize only the first input records.')
        for position, (group, entry) in enumerate(zip(agenda, entries), 1):
            refs = [Ref.parse(ref) for ref in merge_refs(group['refs'])]
            if any(not any(ref.contains(fact) for ref in refs) for fact in entry['facts']):
                raise TaskValidationError(
                    f'Summary coverage mismatches entry {position}; cite evidence from its corresponding matter.')

    @staticmethod
    def _worklog_coverage(output, material):
        """Worklogs permit context-only observations; bookkeeping follows actual citations."""
        entries = output.get('entries', output.get('units'))
        coverage = []
        for position, item in enumerate(material['items']):
            address = Ref.parse(item['ref'])
            new = [i for i, entry in enumerate(entries)
                   if any(address.contains(fact) for fact in entry['facts'])]
            previous = [record['id'] for record in material['context']['recorded']
                        if any(address.contains(fact) for fact in record['facts'])] if not new else []
            coverage.append({'item': position, 'new': new, 'previous': previous,
                             **({'ignored': 'operational_context'} if not new and not previous else {})})
        output['coverage'] = coverage

    @staticmethod
    def _worklog_entry(entry, reader, items):
        anchor = Ref.parse(entry['anchor']).canonical
        if anchor not in {Ref.parse(item['ref']).canonical for item in items}:
            raise TaskValidationError('Worklog anchor must identify one complete current observation')
        record = reader.verify(anchor)
        provenance = record.get('provenance') if isinstance(record, dict) else None
        if not provenance or provenance.get('event_kind') in {'tool_call', 'progress', 'delegation'}:
            raise TaskValidationError('Choose an actual message or result as anchor; a tool call proves only an attempt')
        claims = {'user': 'user_statement', 'assistant': 'assistant_report', 'tool': 'tool_observation'}
        if provenance.get('role') not in claims:
            raise TaskValidationError('Worklog attribution requires a known source role')
        if not any(Ref.parse(fact).contains(anchor) for fact in entry['facts']):
            entry['facts'].append(anchor)  # The declared primary evidence is already verified above.
        entry['claim'] = claims[provenance['role']]
        # The observation time is data, not a model guess or the session start.
        stamp = provenance.get('occurred_at')
        entry['time'] = {'precision': 'instant', 'value': stamp} if stamp else {'precision': 'unknown', 'value': None}
        labels = {'user_statement': '用户表示：', 'assistant_report': '助手报告（未独立验证）：',
                  'tool_observation': '工具结果记录：'}
        label = labels[entry['claim']]
        if not entry['body'].startswith(label):
            entry['body'] = label + entry['body']
        entry['provenance'] = {**provenance, 'anchor': anchor, 'claim': entry['claim']}
