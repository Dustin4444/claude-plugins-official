import json
import os

import pytest

from conftest import (
    HOOKS_DIR, VULN_PY, bash_payload, commit_file, edit_payload, git,
    make_repo, metrics_of, run_hook, stop_payload, ups_payload,
)

import reporesolve as rr
import security_reminder_hook as hook



class TestDirsFromCommand:
    def test_cd_and_and(self, workspace):
        ws, repo = workspace
        assert rr.dirs_from_command("cd sub && git commit -m x", str(ws)) == [str(repo)]

    def test_cd_semicolon_subshell(self, workspace):
        ws, repo = workspace
        assert rr.dirs_from_command("(cd sub; git commit -m x)", str(ws)) == [str(repo)]

    def test_git_dash_C(self, workspace):
        ws, repo = workspace
        assert rr.dirs_from_command("git -C sub commit -q -m x", str(ws)) == [str(repo)]

    def test_git_dash_C_absolute_and_quoted(self, tmp_path):
        d = tmp_path / "my repo"
        d.mkdir()
        assert rr.dirs_from_command(f'git -C "{d}" commit -m "a b"', "/nonexistent") == [str(d)]

    def test_env_prefix_and_config_opts(self, workspace):
        ws, repo = workspace
        cmd = "FOO=1 git -c user.name=x -C sub commit -m x"
        assert rr.dirs_from_command(cmd, str(ws)) == [str(repo)]

    def test_git_dir_work_tree(self, workspace):
        ws, repo = workspace
        cmd = "git --git-dir=sub/.git --work-tree=sub commit -m x"
        assert rr.dirs_from_command(cmd, str(ws)) == [str(repo)]
        cmd = "git --git-dir sub/.git commit -m x"
        assert rr.dirs_from_command(cmd, str(ws)) == [str(repo)]

    def test_subcommand_filter(self, workspace):
        ws, repo = workspace
        cmd = "git -C other status && git -C sub commit -m x && git -C third push"
        assert rr.dirs_from_command(cmd, str(ws), rr.COMMIT_SUBCOMMANDS) == [str(repo)]
        assert rr.dirs_from_command(cmd, str(ws), rr.PUSH_SUBCOMMANDS) == [str(ws / "third")]

    def test_gt(self, workspace):
        ws, repo = workspace
        assert rr.dirs_from_command("cd sub && gt create -m x", str(ws), rr.COMMIT_SUBCOMMANDS) == [str(repo)]

    def test_pushd_and_relative_chain(self, workspace):
        ws, repo = workspace
        cmd = "pushd sub && cd .. && cd ./sub && git commit -m x"
        assert rr.dirs_from_command(cmd, str(ws)) == [str(repo)]

    @pytest.mark.parametrize("cmd", [
        'git -C "unterminated commit -m x',
        "git -C=sub commit -m x",
        "cd && git commit",
        "cd - && git commit -m x",
        "git",
        "git -C",
        "cd /definitely/not/here && git commit -m x",
        "echo 'git commit' | cat",
        "git commit -m \"$(cat <<'EOF'\nmsg\nEOF\n)\"",
        "",
        None,
    ])
    def test_odd_inputs_do_not_raise(self, workspace, cmd):
        ws, _ = workspace
        out = rr.dirs_from_command(cmd, str(ws))
        assert isinstance(out, list)
        assert rr.toplevel_from_command(cmd, str(ws)) in (None, str(ws / "sub"))

    def test_toplevel_from_command_nonexistent_dir(self, workspace):
        ws, _ = workspace
        assert rr.toplevel_from_command("cd nope && git commit -m x", str(ws)) is None

    def test_toplevel_from_subdir_of_repo(self, workspace):
        ws, repo = workspace
        (repo / "pkg").mkdir()
        assert rr.toplevel_from_command("cd sub/pkg && git commit -m x", str(ws)) == str(repo)


class TestShaScan:
    def test_finds_repo_containing_commit(self, workspace):
        ws, repo = workspace
        sha, _ = commit_file(repo, "app.py", VULN_PY)
        assert rr.repo_containing_commit(sha[:7], [str(ws)]) == str(repo)
        assert rr.repo_containing_commit(sha, [str(ws)]) == str(repo)

    def test_unknown_sha(self, workspace):
        ws, _ = workspace
        assert rr.repo_containing_commit("deadbeefdeadbeef", [str(ws)]) is None

    def test_invalid_sha(self, workspace):
        ws, _ = workspace
        assert rr.repo_containing_commit("HEAD; rm -rf x", [str(ws)]) is None
        assert rr.repo_containing_commit(None, [str(ws)]) is None

    def test_skips_ignored_dirs_and_respects_depth(self, tmp_path):
        ws = tmp_path / "ws"
        deep = make_repo(ws / "a" / "b" / "c" / "d" / "repo")
        nm = make_repo(ws / "node_modules" / "pkg")
        found = list(rr.iter_git_repos([str(ws)], max_depth=3))
        assert str(deep) not in found and str(nm) not in found
        found = list(rr.iter_git_repos([str(ws)], max_depth=6))
        assert str(deep) in found and str(nm) not in found

    def test_max_repos_cap(self, tmp_path):
        ws = tmp_path / "ws"
        for i in range(4):
            make_repo(ws / f"r{i}")
        assert len(list(rr.iter_git_repos([str(ws)], max_repos=2))) == 2


class TestReposFromPaths:
    def test_groups_by_toplevel_and_orders_by_count(self, tmp_path):
        ws = tmp_path / "ws"
        a = make_repo(ws / "a")
        b = make_repo(ws / "b")
        paths = [str(b / "x.py"), str(a / "one.py"), str(a / "pkg" / "two.py"),
                 str(ws / "loose.txt"), "", None, 42]
        assert rr.repos_from_paths(paths, str(ws)) == [str(a), str(b)]

    def test_relative_paths_resolve_against_cwd(self, workspace):
        ws, repo = workspace
        assert rr.repos_from_paths(["sub/app.py"], str(ws)) == [str(repo)]

    def test_missing_parent_dirs(self, workspace):
        ws, repo = workspace
        assert rr.repos_from_paths([str(repo / "new" / "deeper" / "f.py")], str(ws)) == [str(repo)]


class TestResolveRepoRoot:
    def test_cwd_is_repo(self, workspace):
        ws, repo = workspace
        assert rr.resolve_repo_root(str(repo), "cd /tmp && git commit") == (str(repo), rr.RES_CWD)

    def test_order_command_then_paths_then_sha(self, workspace):
        ws, repo = workspace
        other = make_repo(ws / "other")
        sha, _ = commit_file(repo, "app.py", VULN_PY)
        assert rr.resolve_repo_root(str(ws), "git -C other commit", rr.COMMIT_SUBCOMMANDS,
                                    sha=sha, touched_paths=[str(repo / "app.py")]) == (str(other), rr.RES_COMMAND)
        assert rr.resolve_repo_root(str(ws), "git commit", rr.COMMIT_SUBCOMMANDS,
                                    sha=sha, touched_paths=[str(repo / "app.py")]) == (str(repo), rr.RES_TOUCHED_PATHS)
        assert rr.resolve_repo_root(str(ws), "git commit", rr.COMMIT_SUBCOMMANDS,
                                    sha=sha) == (str(repo), rr.RES_SHA_SCAN)
        assert rr.resolve_repo_root(str(ws), "git commit") == (None, rr.RES_NONE)


class TestRegexes:
    @pytest.mark.parametrize("cmd", [
        "git commit -m x",
        "git -C sub commit -m x",
        'git -C "a b" commit -m x',
        "git -c core.editor=true -C sub commit --amend",
        "git --git-dir=x/.git --work-tree=x commit -m x",
        "git --git-dir x/.git commit -m x",
        "cd sub && git commit -m x",
        "gt create -m x",
    ])
    def test_commit_re(self, cmd):
        assert hook._GIT_COMMIT_RE.search(cmd)

    @pytest.mark.parametrize("cmd", [
        "git push", "git -C sub push origin main", 'git -C "a b" push',
        "git --work-tree x push -u origin HEAD", "gt submit",
    ])
    def test_push_re(self, cmd):
        assert hook._GIT_PUSH_RE.search(cmd)

    @pytest.mark.parametrize("cmd", ["git status", "git log --oneline", "echo commit"])
    def test_commit_re_negative(self, cmd):
        assert not hook._GIT_COMMIT_RE.search(cmd)


class TestHooksJson:
    def test_matchers_and_events(self):
        cfg = json.loads((HOOKS_DIR / "hooks.json").read_text())
        assert "SubagentStop" in cfg["hooks"]
        assert cfg["hooks"]["SubagentStop"][0]["hooks"][0]["command"] == \
            cfg["hooks"]["Stop"][0]["hooks"][0]["command"]
        bash = [g for g in cfg["hooks"]["PostToolUse"] if g.get("matcher") == "Bash"][0]
        ifs = {h.get("if") for h in bash["hooks"]}
        assert {"Bash(git commit:*)", "Bash(git push:*)", "Bash(git -C * commit*)",
                "Bash(git -C * push*)", "Bash(gt create:*)", "Bash(gt modify:*)",
                "Bash(gt submit:*)"} <= ifs



def _read_state(env):
    d = env["SECURITY_WARNINGS_STATE_DIR"]
    files = [e.path for e in os.scandir(d) if e.name.endswith(".json")]
    assert files, os.listdir(d)
    with open(files[0]) as f:
        return json.load(f)


class TestCommitReview:
    def test_cwd_is_repo_unchanged(self, workspace, hook_env, stub_api):
        ws, repo = workspace
        sha, out = commit_file(repo, "app.py", VULN_PY)
        rc, so, se = run_hook(bash_payload(repo, "git commit -m change", out), hook_env)
        m = metrics_of(so)
        assert m["commit_review"] is True
        assert "cwd_is_repo" not in m and "repo_resolution" not in m
        assert m.get("files_reviewed") == 1 and m.get("skip_reason") is None
        assert stub_api.calls

    def test_workspace_cwd_cd_sub(self, workspace, hook_env, stub_api):
        ws, repo = workspace
        sha, out = commit_file(repo, "app.py", VULN_PY)
        rc, so, se = run_hook(bash_payload(ws, "cd sub && git commit -m change", out), hook_env)
        m = metrics_of(so)
        assert m.get("skip_reason") is None, m
        assert m["cwd_is_repo"] is False and m["repo_resolution"] == rr.RES_COMMAND
        assert m["files_reviewed"] == 1
        assert stub_api.calls

    def test_workspace_cwd_git_dash_C_quiet_uses_reflog(self, workspace, hook_env, stub_api):
        ws, repo = workspace
        sha, _ = commit_file(repo, "app.py", VULN_PY)
        rc, so, se = run_hook(bash_payload(ws, "git -C sub commit -q -m change", ""), hook_env)
        m = metrics_of(so)
        assert m.get("skip_reason") is None, m
        assert m["repo_resolution"] == rr.RES_COMMAND and m["sha_via_reflog"] is True
        assert m["files_reviewed"] == 1

    def test_workspace_cwd_sha_scan(self, workspace, hook_env, stub_api):
        ws, repo = workspace
        sha, out = commit_file(repo, "app.py", VULN_PY)
        rc, so, se = run_hook(bash_payload(ws, "git commit -m change", out), hook_env)
        m = metrics_of(so)
        assert m.get("skip_reason") is None, m
        assert m["repo_resolution"] == rr.RES_SHA_SCAN and m["files_reviewed"] == 1

    def test_workspace_cwd_sha_scan_via_project_dir(self, workspace, hook_env, tmp_path):
        ws, repo = workspace
        sha, out = commit_file(repo, "app.py", VULN_PY)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        env = {**hook_env, "CLAUDE_PROJECT_DIR": str(ws)}
        rc, so, se = run_hook(bash_payload(elsewhere, "git commit -m change", out), env)
        m = metrics_of(so)
        assert m.get("skip_reason") is None, m
        assert m["repo_resolution"] == rr.RES_SHA_SCAN

    def test_hint_saved_and_used(self, workspace, hook_env):
        ws, repo = workspace
        sha, out = commit_file(repo, "app.py", VULN_PY)
        run_hook(bash_payload(ws, "cd sub && git commit -m change", out), hook_env)
        state = _read_state(hook_env)
        assert state.get("repo_root_hint") == str(repo)
        assert rr.resolve_repo_root(str(ws), "git commit") == (None, rr.RES_NONE)
        os.environ["SECURITY_WARNINGS_STATE_DIR"] = hook_env["SECURITY_WARNINGS_STATE_DIR"]
        try:
            assert rr.load_repo_hint("s1") == str(repo)
        finally:
            os.environ.pop("SECURITY_WARNINGS_STATE_DIR", None)

    def test_hint_used_for_quiet_commit_without_dir(self, workspace, hook_env):
        ws, repo = workspace
        sha, out = commit_file(repo, "app.py", VULN_PY)
        run_hook(bash_payload(ws, "cd sub && git commit -m change", out), hook_env)
        commit_file(repo, "app.py", VULN_PY + "# 2\n")
        rc, so, se = run_hook(bash_payload(ws, "git commit -q -m change", ""), hook_env)
        m = metrics_of(so)
        assert m.get("skip_reason") is None, m
        assert m["repo_resolution"] == rr.RES_HINT and m["sha_via_reflog"] is True

    def test_unresolvable_still_skips_26(self, workspace, hook_env, tmp_path):
        ws, repo = workspace
        sha, out = commit_file(repo, "app.py", VULN_PY)
        empty = tmp_path / "empty"
        empty.mkdir()
        rc, so, se = run_hook(bash_payload(empty, "git commit -m change", out), hook_env)
        m = metrics_of(so)
        assert m["skip_reason"] == 26 and m["cwd_is_repo"] is False and m["repo_resolution"] == rr.RES_NONE

    def test_no_credentials_gate_precedes_repo_resolution(self, workspace, hook_env):
        ws, repo = workspace
        sha, out = commit_file(repo, "app.py", VULN_PY)
        env = {k: v for k, v in hook_env.items() if k != "ANTHROPIC_API_KEY"}
        rc, so, se = run_hook(bash_payload(ws, "cd sub && git commit -m change", out), env)
        m = metrics_of(so)
        assert m["skip_reason"] == 22 and m["repo_resolution"] == rr.RES_COMMAND

    def test_dedup_sentinel_uses_resolved_repo(self, workspace, hook_env):
        ws, repo = workspace
        sha, out = commit_file(repo, "app.py", VULN_PY)
        p = bash_payload(ws, "git -C sub commit -m change && git -C sub push", out, tool_use_id="toolu_1")
        rc1, so1, _ = run_hook(p, hook_env)
        rc2, so2, _ = run_hook(p, hook_env)
        assert metrics_of(so1).get("commit_review") is True
        assert metrics_of(so2) == {"bash_hook_dedup": True}


class TestPushSweep:
    def test_workspace_cwd_git_dash_C_push(self, workspace, hook_env, tmp_path):
        ws, repo = workspace
        remote = tmp_path / "remote.git"
        git(ws, "init", "-q", "--bare", str(remote))
        git(repo, "remote", "add", "origin", str(remote))
        git(repo, "push", "-q", "-u", "origin", "main")
        base = git(repo, "rev-parse", "HEAD").strip()
        sha, _ = commit_file(repo, "app.py", VULN_PY)
        out = git(repo, "push", "--porcelain", "origin", "main")
        push_stdout = f"To {remote}\n   {base[:7]}..{sha[:7]}  main -> main\n"
        rc, so, se = run_hook(bash_payload(ws, "git -C sub push origin main", push_stdout), hook_env)
        m = metrics_of(so)
        assert m["push_sweep"] is True
        assert m["cwd_is_repo"] is False and m["repo_resolution"] == rr.RES_COMMAND
        assert m.get("skip_reason") != 26
        assert m.get("pushed") == 1


class TestStop:
    def _touch(self, ws, repo, hook_env, session_id="s1"):
        (repo / "app.py").write_text(VULN_PY)
        run_hook(edit_payload(ws, repo / "app.py", VULN_PY, session_id=session_id), hook_env)

    def test_cwd_is_repo_unchanged(self, workspace, hook_env, stub_api):
        ws, repo = workspace
        run_hook(ups_payload(repo), hook_env)
        self._touch(repo, repo, hook_env)
        rc, so, se = run_hook(stop_payload(repo), hook_env)
        m = metrics_of(so)
        assert m.get("skip_reason") is None, m
        assert "repo_resolution" not in m and m["files_reviewed"] == 1
        assert stub_api.calls

    def test_workspace_cwd_resolves_from_touched_paths(self, workspace, hook_env, stub_api):
        ws, repo = workspace
        run_hook(ups_payload(ws), hook_env)
        self._touch(ws, repo, hook_env)
        rc, so, se = run_hook(stop_payload(ws), hook_env)
        m = metrics_of(so)
        assert m.get("skip_reason") is None, m
        assert m["cwd_is_repo"] is False and m["repo_resolution"] == rr.RES_TOUCHED_PATHS
        assert m["files_reviewed"] == 1 and m["review_set_count"] == 1
        assert stub_api.calls

    def test_subagent_stop_is_handled_and_advances_baseline(self, workspace, hook_env, stub_api):
        ws, repo = workspace
        run_hook(ups_payload(ws), hook_env)
        self._touch(ws, repo, hook_env)
        rc, so, se = run_hook(stop_payload(ws, event="SubagentStop"), hook_env)
        m = metrics_of(so)
        assert m.get("skip_reason") is None, m
        assert m["repo_resolution"] == rr.RES_TOUCHED_PATHS and m["files_reviewed"] == 1
        n_calls = len(stub_api.calls)
        rc, so, se = run_hook(stop_payload(ws), hook_env)
        m2 = metrics_of(so)
        assert m2["skip_reason"] in (6, 9), m2
        assert m2["repo_resolution"] == rr.RES_HINT
        assert len(stub_api.calls) == n_calls

    def test_ups_uses_hint_for_baseline(self, workspace, hook_env):
        ws, repo = workspace
        run_hook(ups_payload(ws), hook_env)
        self._touch(ws, repo, hook_env)
        run_hook(stop_payload(ws, event="SubagentStop"), hook_env)
        (repo / "app.py").write_text(VULN_PY + "\n# more\n")
        run_hook(ups_payload(ws, session_id="s1"), hook_env)
        state = _read_state(hook_env)
        assert state.get("baseline_sha") and state.get("repo_root_hint") == str(repo)

    def test_workspace_cwd_nothing_touched_skips(self, workspace, hook_env, stub_api):
        ws, repo = workspace
        run_hook(ups_payload(ws), hook_env)
        rc, so, se = run_hook(stop_payload(ws), hook_env)
        m = metrics_of(so)
        assert m["skip_reason"] == 9 and m["repo_resolution"] == rr.RES_NONE
        assert not stub_api.calls
