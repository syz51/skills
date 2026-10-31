"""Regression checks for real-content activation evidence, without model calls."""
import hashlib
import json
from pathlib import Path
import shlex
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
from activation_trace import SOURCE_SHA256, node_read_observations, score_trace
from read_guidance import PROTOCOL


TS = '/fixture/skills/typescript/SKILL.md'
UI = '/fixture/skills/frontend-ui/SKILL.md'
CONTENT = '---\nname: typescript\n---\n# TypeScript\nUnknown inputs: 中文🙂\n'
UI_CONTENT = '# Frontend and UI\nRead the user workflow first.\n'


def trace(*items, complete=True):
    events = []
    for item in items:
        events.append(item if 'type' in item and item['type'].startswith(('turn.', 'item.')) else {'type': 'item.completed', 'item': item})
    if complete:
        events.append({'type': 'turn.completed', 'usage': {}})
    return '\n'.join(json.dumps(item, ensure_ascii=False) for item in events)


def command(cmd, output=CONTENT, exit_code=0, **extra):
    return {'type': 'command_execution', 'command': cmd, 'exit_code': exit_code, 'aggregated_output': output, **extra}


def node(code, output=CONTENT, **extra):
    return {'type': 'mcp_tool_call', 'server': 'node_repl', 'tool': 'js', 'status': 'completed',
            'arguments': {'code': code}, 'result': {'content': [{'type': 'text', 'text': output}]}, 'error': None, **extra}


def envelope(content, start=0, next_start=None, **extra):
    return {'protocol': PROTOCOL, 'path': 'typescript/SKILL.md', 'start': start, 'next_start': next_start,
            'coverage': 'complete' if start == 0 and next_start is None else 'partial',
            'sha256': hashlib.sha256(content.encode()).hexdigest(), 'content': content, **extra}


class ActivationTraceTests(unittest.TestCase):
    def setUp(self):
        self.targets = {'typescript': {'path': TS, 'content': CONTENT},
                        'frontend-ui': {'path': UI, 'content': UI_CONTENT}}

    def score(self, value, required=('typescript',), forbidden=(), before=()):
        return score_trace(value, self.targets, required, forbidden, before)

    def test_literal_cat_and_shell_wrapper_observe_exact_full_bytes(self):
        for cmd in ('cat ' + TS, '/bin/zsh -lc ' + shlex.quote('cat ' + TS)):
            with self.subTest(cmd=cmd):
                result = self.score(trace(command(cmd)))
                self.assertTrue(result['passed'], result)
                [read] = result['reads']
                self.assertEqual(read['sha256'], hashlib.sha256(CONTENT.encode()).hexdigest())
                self.assertEqual(read['coverage'], 'complete')

    def test_successful_read_only_inventory_prefix_attributes_final_cat_suffix(self):
        inner = "pwd && rg --files -g 'AGENTS.md' -g '*.ts' -g '!node_modules' . && cat " + TS
        for cmd in (inner, '/bin/zsh -lc ' + shlex.quote(inner), 'ls -la . && cat ' + TS):
            with self.subTest(command=cmd):
                result = self.score(trace(command(cmd, '/fixture\npackage.json\nsrc/index.ts\n' + CONTENT)))
                self.assertTrue(result['passed'], result)
        helper = 'pwd && uv run --script scripts/read_guidance.py ' + TS
        self.assertTrue(self.score(trace(command(helper, '/fixture\n' + json.dumps(envelope(CONTENT)) + '\n')))['passed'])

    def test_arbitrary_compound_prefix_cannot_forge_a_final_read(self):
        for prefix in ("printf 'invented'", 'touch /tmp/changed', 'rg --files --pre malicious .',
                       "echo \"$(cat /tmp/copied)\"", 'pwd; true', 'pwd || true', 'pwd | cat',
                       'ls > /tmp/output', "pwd && printf '# fake'"):
            with self.subTest(prefix=prefix):
                self.assertFalse(self.score(trace(command(prefix + ' && cat ' + TS, CONTENT)))['passed'])

    def test_quoted_unicode_paths_and_explicit_aliases(self):
        alias = '/archived/skills/类型 script/SKILL.md'
        self.targets['typescript']['aliases'] = [alias]
        self.assertTrue(self.score(trace(command('cat ' + shlex.quote(alias))))['passed'])

    def test_alias_is_not_inferred_from_the_basename_or_skill_name(self):
        for path in ('/other/skills/typescript/SKILL.md', '/project/typescript/SKILL.md',
                     '/project/node_modules/typescript/lib/typescript.js'):
            with self.subTest(path=path):
                self.assertFalse(self.score(trace(command('cat ' + path)))['passed'])

    def test_started_failed_or_prose_only_reads_are_not_proof(self):
        cases = [
            trace({'type': 'item.started', 'item': command('cat ' + TS)}),
            trace(command('cat ' + TS, exit_code=1)),
            trace(command('printf %s ' + shlex.quote(CONTENT))),
            trace({'type': 'agent_message', 'text': 'I loaded ' + TS + '\n' + CONTENT}),
        ]
        for value in cases:
            with self.subTest(value=value):
                self.assertFalse(self.score(value)['passed'])

    def test_partial_or_extra_shell_output_does_not_claim_a_full_file(self):
        for output in (CONTENT[:20], 'prefix\n' + CONTENT, CONTENT + 'suffix', 'Warning: truncated output\n' + CONTENT[:30]):
            with self.subTest(output=output):
                self.assertFalse(self.score(trace(command('cat ' + TS, output)))['passed'])

    def test_cat_batch_requires_all_paths_and_exact_concatenation(self):
        good = self.score(trace(command('cat ' + UI + ' ' + TS, UI_CONTENT + CONTENT)), ('frontend-ui', 'typescript'))
        self.assertTrue(good['passed'], good)
        bad = self.score(trace(command('cat /unknown ' + TS, CONTENT)))
        self.assertFalse(bad['passed'])

    def test_helper_complete_envelope_is_bound_to_path_and_content(self):
        cmd = 'uv run --script scripts/read_guidance.py ' + TS
        result = self.score(trace(command(cmd, json.dumps(envelope(CONTENT)))))
        self.assertTrue(result['passed'], result)
        for changed in ({'path': 'frontend-ui/SKILL.md'}, {'sha256': '0' * 64},
                        {'content': CONTENT[:-1]}, {'start': 3}, {'coverage': 'partial'}, {'extra': True}):
            with self.subTest(changed=changed):
                self.assertFalse(self.score(trace(command(cmd, json.dumps({**envelope(CONTENT), **changed}))))['passed'])

    def test_verified_helper_continuations_establish_coverage_without_synthetic_hash(self):
        middle = 26
        a = envelope(CONTENT[:middle], next_start=middle)
        b = envelope(CONTENT[middle:], start=middle)
        cmd = 'uv run --script scripts/read_guidance.py ' + TS
        first = command(cmd, json.dumps(a))
        second = command(cmd + ' --start ' + str(middle), json.dumps(b))
        result = self.score(trace(first, second))
        self.assertTrue(result['passed'], result)
        self.assertEqual([r['coverage'] for r in result['reads']], ['partial', 'partial'])
        self.assertNotIn(hashlib.sha256(CONTENT.encode()).hexdigest(), [r['sha256'] for r in result['reads']])
        self.assertFalse(self.score(trace(second))['passed'])
        self.assertFalse(self.score(trace(first, first))['passed'])

    def test_wrong_start_and_gap_cannot_complete_coverage(self):
        middle = 20
        cmd = 'uv run --script scripts/read_guidance.py ' + TS
        wrong = envelope(CONTENT[middle:], start=middle)
        self.assertFalse(self.score(trace(command(cmd, json.dumps(wrong))))['passed'])
        first = command(cmd, json.dumps(envelope(CONTENT[:middle], next_start=middle)))
        second = command(cmd + ' --start 21', json.dumps(envelope(CONTENT[21:], start=21)))
        self.assertFalse(self.score(trace(first, second))['passed'])

    def test_sed_line_ranges_retain_partial_hashes(self):
        chunks = CONTENT.splitlines(keepends=True)
        a = command("sed -n '1,3p' " + TS, ''.join(chunks[:3]))
        b = command("sed -n '4,20p' " + TS, ''.join(chunks[3:]))
        self.assertTrue(self.score(trace(a, b))['passed'])
        self.assertFalse(self.score(trace(b))['passed'])

    def test_required_guidance_order_is_complete_before_later_read(self):
        ui, ts = command('cat ' + UI, UI_CONTENT), command('cat ' + TS)
        good = self.score(trace(ui, ts), ('frontend-ui', 'typescript'), before=(('frontend-ui', 'typescript'),))
        self.assertTrue(good['passed'], good)
        self.assertFalse(self.score(trace(ts, ui), ('frontend-ui', 'typescript'), before=(('frontend-ui', 'typescript'),))['passed'])
        batch = command('cat ' + UI + ' ' + TS, UI_CONTENT + CONTENT)
        self.assertFalse(self.score(trace(batch), ('frontend-ui', 'typescript'), before=(('frontend-ui', 'typescript'),))['passed'])

    def test_partial_earlier_read_does_not_establish_order(self):
        # A later full frontend read cannot retroactively precede TypeScript.
        prefix = command("sed -n '1,1p' " + UI, UI_CONTENT.splitlines(keepends=True)[0])
        events = trace(prefix, command('cat ' + TS), command('cat ' + UI, UI_CONTENT))
        self.assertFalse(self.score(events, ('frontend-ui', 'typescript'), before=(('frontend-ui', 'typescript'),))['passed'])

    def test_forbidden_read_detects_partial_content_and_no_read_control_passes(self):
        self.assertTrue(self.score(trace(), (), ('typescript',))['passed'])
        partial = command("sed -n '1,1p' " + TS, CONTENT.splitlines(keepends=True)[0])
        self.assertFalse(self.score(trace(partial), (), ('typescript',))['passed'])
        self.assertFalse(self.score(trace(command('cat ' + TS, CONTENT[:10])), (), ('typescript',))['passed'])

    def test_malformed_json_and_incomplete_or_failed_turn_cannot_pass_even_a_negative(self):
        for value in ('not json\n' + trace(), '[]\n' + trace(), trace(complete=False),
                      trace({'type': 'turn.failed', 'error': {'message': 'failed'}})):
            with self.subTest(value=value):
                self.assertFalse(self.score(value, (), ('typescript',))['passed'])

    def test_duplicate_events_do_not_forge_continuation_or_order(self):
        item = command('cat ' + TS, id='same')
        result = self.score(trace(item, item))
        self.assertTrue(result['passed'], result)
        self.assertEqual(len(result['reads']), 1)
        self.assertFalse(self.score(trace(item, {**item, 'aggregated_output': 'changed'}))['passed'])

    def test_scorer_uses_supplied_historical_snapshot_without_opening_a_file(self):
        # The path deliberately does not exist in the test environment.
        self.assertTrue(self.score(trace(command('cat ' + TS)))['passed'])
        current = CONTENT + 'today\n'
        self.assertFalse(self.score(trace(command('cat ' + TS, current)))['passed'])

    def test_node_direct_read_and_literal_binding_are_complete_evidence(self):
        for code in (
            "var fs = await import('node:fs/promises'); nodeRepl.write(await fs.readFile('" + TS + "', 'utf8'));",
            "const fs = await import('node:fs/promises'); const p = '" + TS + "'; const text = await fs.readFile(p, 'utf-8'); nodeRepl.write(text);",
        ):
            with self.subTest(code=code):
                result = self.score(trace(node(code)))
                self.assertTrue(result['passed'], result)

    def test_node_fs_bindings_persist_only_across_supported_completed_calls(self):
        imported = node("var fs = await import('node:fs/promises');", '')
        read = node("nodeRepl.write(await fs.readFile('" + TS + "', 'utf8'));")
        self.assertTrue(self.score(trace(imported, read))['passed'])
        failed = node("var fs = await import('node:fs/promises');", '', error={'message': 'failed'})
        self.assertFalse(self.score(trace(failed, read))['passed'])
        changed = node("fs = {readFile: async () => 'fake'};", '')
        self.assertFalse(self.score(trace(imported, changed, read))['passed'])

    def test_node_slice_continuations_observe_exact_offsets(self):
        imported = node("var fs = await import('node:fs/promises');", '')
        prefix = node("nodeRepl.write((await fs.readFile('" + TS + "', 'utf8')).slice(0, 25));", CONTENT[:25])
        rest = node("nodeRepl.write((await fs.readFile('" + TS + "', 'utf8')).slice(25));", CONTENT[25:])
        self.assertTrue(self.score(trace(imported, prefix, rest))['passed'])
        self.assertFalse(self.score(trace(imported, rest))['passed'])

    def test_node_slice_offsets_follow_utf16_not_python_codepoint_indices(self):
        imported = node("var fs = await import('node:fs/promises');", '')
        prefix = node("nodeRepl.write((await fs.readFile('" + TS + "', 'utf8')).slice(0, -3));", CONTENT[:-2])
        rest = node("nodeRepl.write((await fs.readFile('" + TS + "', 'utf8')).slice(-3));", '🙂\n')
        self.assertTrue(self.score(trace(imported, prefix, rest))['passed'])
        wrong = node("nodeRepl.write((await fs.readFile('" + TS + "', 'utf8')).slice(-3));", CONTENT[-3:])
        self.assertFalse(self.score(trace(imported, prefix, wrong))['passed'])

    def test_inspect_string_concatenation_is_decoded_as_data(self):
        code = "var fs = await import('node:fs/promises'); nodeRepl.write(await fs.readFile('" + TS + "', 'utf8'));"
        output = repr(CONTENT[:25]) + ' +\n' + repr(CONTENT[25:])
        self.assertTrue(self.score(trace(node(code, output)))['passed'])
        self.assertFalse(self.score(trace(node(code, repr(CONTENT) + ' malformed()')))['passed'])

    def test_node_literal_batch_attributes_fulfilled_results_by_index(self):
        code = ("var fs = await import('node:fs/promises'); var paths = ['" + UI + "', '" + TS + "']; "
                "var reads = await Promise.allSettled(paths.map(p => fs.readFile(p, 'utf8'))); nodeRepl.write(reads);")
        output = '[{status: \'fulfilled\', value: ' + repr(UI_CONTENT) + '}, {status: \'fulfilled\', value: ' + repr(CONTENT) + '}]'
        result = self.score(trace(node(code, output)), ('typescript', 'frontend-ui'))
        self.assertTrue(result['passed'], result)
        failed = output.replace("status: 'fulfilled'", "status: 'rejected'", 1)
        self.assertFalse(self.score(trace(node(code, failed)), ('typescript', 'frontend-ui'))['passed'])
        swapped = '[{status: \'fulfilled\', value: ' + repr(CONTENT) + '}, {status: \'fulfilled\', value: ' + repr(UI_CONTENT) + '}]'
        self.assertFalse(self.score(trace(node(code, swapped)), ('typescript', 'frontend-ui'))['passed'])

    def test_node_labelled_batch_requires_unchanged_read_dataflow(self):
        code = ("var fs = await import('node:fs/promises'); var paths = ['" + TS + "']; "
                "var reads = await Promise.allSettled(paths.map(p => fs.readFile(p, 'utf8'))); "
                "for (var i=0;i<reads.length;i++){nodeRepl.write('FILE: '+paths[i]); nodeRepl.write(reads[i].status==='fulfilled'?reads[i].value:String(reads[i].reason));}")
        self.assertTrue(self.score(trace(node(code, 'FILE: ' + TS + CONTENT)))['passed'])
        forged = code.replace('reads[i].value', repr(CONTENT))
        self.assertFalse(self.score(trace(node(forged, 'FILE: ' + TS + CONTENT)))['passed'])

    def test_path_mentions_comments_or_printing_a_copy_are_not_node_read_evidence(self):
        for code in (
            "// await fs.readFile('" + TS + "','utf8');\nnodeRepl.write(" + json.dumps(CONTENT) + ");",
            "nodeRepl.write('" + TS + "'); nodeRepl.write(" + json.dumps(CONTENT) + ");",
            "var fs=await import('node:fs/promises'); var text=await fs.readFile('" + TS + "','utf8'); nodeRepl.write(" + json.dumps(CONTENT) + ");",
            "var fs=await import('node:fs/promises'); if(false){nodeRepl.write(await fs.readFile('" + TS + "','utf8'));}",
        ):
            with self.subTest(code=code):
                self.assertFalse(self.score(trace(node(code)))['passed'])
        comment = node("// fs.readFile('" + TS + "', 'utf8');\nnodeRepl.write('example');", 'example')
        self.assertTrue(self.score(trace(comment), (), ('typescript',))['passed'])

    def test_unattributed_node_read_cannot_make_a_negative_control_pass(self):
        code = "var fs=await import('node:fs/promises'); if (true) { nodeRepl.write(await fs.readFile('" + TS + "','utf8')); }"
        result = self.score(trace(node(code)), (), ('typescript',))
        self.assertFalse(result['passed'])
        self.assertTrue(any('Unverified' in failure for failure in result['failures']))

    def test_computed_node_paths_and_unknown_transformations_cannot_hide_forbidden_guidance(self):
        for expression in ("'/fixture/skills/'+'typescript/SKILL.md'", "path.join('/fixture/skills','typescript','SKILL.md')", 'computedPath'):
            code = "var fs=await import('node:fs/promises'); if(true){nodeRepl.write(await fs.readFile(" + expression + ",'utf8'));}"
            with self.subTest(expression=expression):
                result = self.score(trace(node(code)), (), ('typescript',))
                self.assertFalse(result['passed'], result)
                self.assertEqual(result['reads'], [])
        unrelated = "var fs=await import('node:fs/promises'); if(true){nodeRepl.write(await fs.readFile('/fixture/README.md','utf8'));}"
        self.assertTrue(self.score(trace(node(unrelated, 'An unrelated local document.')), (), ('typescript',))['passed'])

    def test_unverified_earlier_read_cannot_be_repaired_into_an_ordering_claim(self):
        code = "var fs=await import('node:fs/promises'); if(true){nodeRepl.write(await fs.readFile('/fixture/skills/'+'typescript/SKILL.md','utf8'));}"
        events = trace(node(code), command('cat ' + UI, UI_CONTENT), command('cat ' + TS))
        result = self.score(events, ('frontend-ui', 'typescript'), before=(('frontend-ui', 'typescript'),))
        self.assertFalse(result['passed'], result)

    def test_extra_or_truncated_node_output_does_not_prove_single_read(self):
        code = "var fs=await import('node:fs/promises'); nodeRepl.write(await fs.readFile('" + TS + "','utf8'));"
        for output in ('prefix' + CONTENT, CONTENT + 'suffix', CONTENT[:20], 'Warning: truncated output\n' + CONTENT):
            with self.subTest(output=output):
                self.assertFalse(self.score(trace(node(code, output)))['passed'])

    def test_unsupported_shell_forms_cannot_hide_complete_forbidden_content(self):
        for cmd in ('p=' + TS + '; cat "$p"', 'head -n 9999 ' + TS, 'printf %s ' + shlex.quote(CONTENT)):
            with self.subTest(command=cmd):
                result = self.score(trace(command(cmd)), (), ('typescript',))
                self.assertFalse(result['passed'], result)
                self.assertEqual(result['reads'], [])
        self.assertTrue(self.score(trace(command('head -n 3 /fixture/README.md', 'An unrelated file.')), (), ('typescript',))['passed'])
        mention = trace(command('printf %s ' + shlex.quote(TS), TS + '\n# TypeScript'))
        self.assertTrue(self.score(mention, (), ('typescript',))['passed'])

    def test_production_node_adapter_preserves_observed_bytes_and_session_bindings(self):
        state = {}
        self.assertEqual(node_read_observations("var fs=await import('node:fs/promises');", '', state), [])
        code = "var body=await fs.readFile('" + TS + "','utf8'); nodeRepl.write(body);"
        [read] = node_read_observations(code, CONTENT, state)
        self.assertEqual(read, {'path': TS, 'content': CONTENT, 'start': 0, 'coverage': 'complete'})
        partial = "nodeRepl.write((await fs.readFile('" + TS + "','utf8')).slice(20));"
        self.assertEqual(node_read_observations(partial, CONTENT[20:], state),
                         [{'path': TS, 'content': CONTENT[20:], 'start': 20, 'coverage': 'partial'}])
        self.assertEqual(node_read_observations("fs = fake();", CONTENT, state), [])
        self.assertEqual(state, {})
        self.assertEqual(node_read_observations(code, CONTENT, state), [])
        self.assertRegex(SOURCE_SHA256, r'^[a-f0-9]{64}$')

    def test_production_node_adapter_rejects_copied_or_unattributable_output(self):
        copied = "var fs=await import('node:fs/promises'); nodeRepl.write(" + json.dumps(CONTENT) + ");"
        self.assertEqual(node_read_observations(copied, CONTENT, {}), [])
        conditional = "var fs=await import('node:fs/promises'); if(true){nodeRepl.write(await fs.readFile('" + TS + "','utf8'));}"
        self.assertEqual(node_read_observations(conditional, CONTENT, {}), [])
        code = "var fs=await import('node:fs/promises'); nodeRepl.write(await fs.readFile('" + TS + "','utf8'));"
        self.assertEqual(node_read_observations(code, '\xff\x00', {}), [])

    def test_production_node_adapter_attributes_only_fulfilled_batch_slots(self):
        code = ("var fs=await import('node:fs/promises'); var paths=['" + UI + "','" + TS + "']; "
                "var reads=await Promise.allSettled(paths.map(p=>fs.readFile(p,'utf8'))); nodeRepl.write(reads);")
        output = '[{status: \'rejected\', reason: \'ENOENT\'}, {status: \'fulfilled\', value: ' + repr(CONTENT) + '}]'
        self.assertEqual(node_read_observations(code, output, {}),
                         [{'path': TS, 'content': CONTENT, 'start': 0, 'coverage': 'complete'}])

    def test_failed_or_started_mcp_result_is_not_evidence(self):
        code = "var fs=await import('node:fs/promises'); nodeRepl.write(await fs.readFile('" + TS + "','utf8'));"
        for item in (node(code, status='failed'), node(code, error='failed'),
                     node(code, result={'isError': True, 'content': [{'type': 'text', 'text': CONTENT}]})):
            self.assertFalse(self.score(trace(item))['passed'])
        self.assertFalse(self.score(trace({'type': 'item.started', 'item': node(code)}))['passed'])

    def test_known_mcp_shell_result_and_unchanged_exec_wrapper(self):
        result = {'content': [{'type': 'text', 'text': json.dumps({'exit_code': 0, 'output': CONTENT})}]}
        item = {'type': 'mcp_tool_call', 'server': 'functions', 'tool': 'exec_command', 'status': 'completed',
                'arguments': {'cmd': 'cat ' + TS}, 'result': result, 'error': None}
        self.assertTrue(self.score(trace(item))['passed'])
        code = 'text(await tools.exec_command({cmd: ' + json.dumps('cat ' + TS) + '}));'
        wrapped = {**item, 'tool': 'exec', 'arguments': {'code': code}}
        self.assertTrue(self.score(trace(wrapped))['passed'])
        forged = {**wrapped, 'arguments': {'code': 'text({exit_code:0,output:' + json.dumps(CONTENT) + '}); /* ' + code + ' */'}}
        self.assertFalse(self.score(trace(forged))['passed'])
        self.assertFalse(self.score(trace({**item, 'server': 'unrelated-plugin'}))['passed'])
        hidden = {**item, 'result': {'content': [{'type': 'text', 'text': 'Action completed.'}],
                                   'structuredContent': {'exit_code': 0, 'output': CONTENT}}}
        self.assertFalse(self.score(trace(hidden))['passed'])

    def test_invalid_configuration_fails_at_the_boundary(self):
        with self.assertRaises(ValueError):
            self.score(trace(), ('unknown',))
        for changed in ({'content': ''}, {'content': 'bad\x00bytes'}, {'content': '\ud800'},
                        {'aliases': 'not a list'}, {'path': '/work/../SKILL.md'}):
            with self.subTest(changed=changed):
                targets = {'typescript': {**self.targets['typescript'], **changed}}
                with self.assertRaises(ValueError):
                    score_trace(trace(), targets, ('typescript',))

    def test_invalid_tool_identity_is_a_failure_not_a_parser_crash(self):
        for identity in ({'unhashable': True}, [], False, ''):
            with self.subTest(identity=identity):
                self.assertFalse(self.score(trace(command('cat ' + TS, id=identity)))['passed'])
