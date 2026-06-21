"""Unit tests for spyro eval — PHP code generation."""

from __future__ import annotations

from spyro.cli.commands import build_eval_php


def test_build_eval_php_basic():
    """Simple expression generates valid PHP with print_r."""
    code = build_eval_php("User::count()")
    assert code.startswith("<?php\n")
    assert "getcwd()" in code
    assert "bootstrap/app.php" in code
    assert "Illuminate\\Contracts\\Console\\Kernel" in code
    assert "print_r((function() { return User::count(); })(), true)" in code
    assert code.endswith('echo "\\n";\n')


def test_build_eval_php_json():
    """--json wraps expression in json_encode instead of print_r."""
    code = build_eval_php("User::count()", json_output=True)
    assert "json_encode" in code
    assert "print_r" not in code
    assert "JSON_PRETTY_PRINT" in code
    assert "JSON_UNESCAPED_SLASHES" in code
    assert "JSON_UNESCAPED_UNICODE" in code
    assert "(function() { return User::count(); })()" in code


def test_build_eval_php_complex_expression():
    """Complex multi-step expressions are preserved verbatim."""
    expr = 'BillCountryProvider::where("enabled", true)->get(["country_code", "provider_name"])->toArray()'
    code = build_eval_php(expr)
    assert expr in code
    assert "print_r(" in code


def test_build_eval_php_namespaced_class():
    """Backslash in namespace expressions is preserved."""
    code = build_eval_php('App\\Models\\User::first()->toArray()')
    assert "App\\\\Models\\\\User" in code or "App\\Models\\User" in code
    assert "User::first()->toArray()" in code


def test_build_eval_php_db_query():
    """DB::table() expression with escaped double quotes."""
    expr = 'DB::table("users")->count()'
    code = build_eval_php(expr, json_output=True)
    assert expr in code
    assert "json_encode((function() { return " + expr + "; })()" in code


def test_build_eval_php_statement_keywords():
    """Keywords like new and -> are preserved."""
    code = build_eval_php("new App\\Models\\User()", json_output=True)
    assert "new App\\\\Models\\\\User()" in code or "new App\\Models\\User()" in code
    assert "json_encode(" in code
