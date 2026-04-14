"""Flask blueprints for each API domain."""

from flask import Blueprint

from . import account, airtags, keys, settings, system, vm


def register(app) -> None:
    for bp in (system.bp, settings.bp, airtags.bp, keys.bp, account.bp, vm.bp):
        app.register_blueprint(bp)


__all__ = ["Blueprint", "register"]
