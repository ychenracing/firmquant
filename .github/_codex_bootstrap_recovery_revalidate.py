from pathlib import Path

path = Path("src/firmquant/strategy/account_bootstrap.py")
text = path.read_text(encoding="utf-8")

old = '''    def _preconditions(self) -> None:\n'''
new = '''    def _preconditions(self, *, allow_existing_account_file: bool = False) -> None:\n'''
if text.count(old) != 1:
    raise SystemExit("preconditions signature marker mismatch")
text = text.replace(old, new)

old = '''        if self._account_path.exists():\n            raise AccountBootstrapDenied("UNBOUND_ACCOUNT_STATE_PRESENT")\n'''
new = '''        if self._account_path.exists() and not allow_existing_account_file:\n            raise AccountBootstrapDenied("UNBOUND_ACCOUNT_STATE_PRESENT")\n'''
if text.count(old) != 1:
    raise SystemExit("preconditions account path marker mismatch")
text = text.replace(old, new)

old = '''    def _recover_file_applied(\n        self,\n        pending: _PendingBootstrap | None,\n    ) -> AccountBootstrapReceipt | None:\n        if pending is None:\n            return None\n        if self._account_path.is_symlink():\n            raise AccountBootstrapDenied("ACCOUNT_STATE_PATH_INVALID")\n        if not self._account_path.exists():\n            if pending.stage == "FILE_COMMITTED":\n                raise AccountBootstrapDenied("ACCOUNT_BOOTSTRAP_CONTRADICTION")\n            return None\n        if not self._account_path.is_file():\n            raise AccountBootstrapDenied("ACCOUNT_STATE_PATH_INVALID")\n        try:\n            actual = self._store.hash_file(self._account_path)\n        except Exception as error:\n            raise AccountBootstrapDenied("ACCOUNT_BOOTSTRAP_CONTRADICTION") from error\n        if actual != pending.account_state_sha256:\n            raise AccountBootstrapDenied("ACCOUNT_BOOTSTRAP_CONTRADICTION")\n        return self._finalize_bootstrap(pending, completed=self._now())\n'''
new = '''    def _recover_file_applied(\n        self,\n        pending: _PendingBootstrap | None,\n        *,\n        snapshot: BrokerSnapshot,\n        identity: StrategyIdentity,\n        data: BootstrapDataIdentity,\n    ) -> AccountBootstrapReceipt | None:\n        if pending is None:\n            return None\n        if self._account_path.is_symlink():\n            raise AccountBootstrapDenied("ACCOUNT_STATE_PATH_INVALID")\n        if not self._account_path.exists():\n            if pending.stage == "FILE_COMMITTED":\n                raise AccountBootstrapDenied("ACCOUNT_BOOTSTRAP_CONTRADICTION")\n            return None\n        if not self._account_path.is_file():\n            raise AccountBootstrapDenied("ACCOUNT_STATE_PATH_INVALID")\n        try:\n            actual = self._store.hash_file(self._account_path)\n        except Exception as error:\n            raise AccountBootstrapDenied("ACCOUNT_BOOTSTRAP_CONTRADICTION") from error\n        if actual != pending.account_state_sha256:\n            raise AccountBootstrapDenied("ACCOUNT_BOOTSTRAP_CONTRADICTION")\n        self._validate_pending_candidate(\n            pending,\n            snapshot=snapshot,\n            identity=identity,\n            data=data,\n            account_state_sha256=actual,\n        )\n        durable_account = self._strict_load(self._account_path)\n        self._validate_seed(\n            durable_account,\n            snapshot=snapshot,\n            identity=identity,\n            data=data,\n        )\n        return self._finalize_bootstrap(pending, completed=self._now())\n'''
if text.count(old) != 1:
    raise SystemExit("recover file marker mismatch")
text = text.replace(old, new)

old = '''        pending = self._pending_bootstrap()\n        recovered = self._recover_file_applied(pending)\n        if recovered is not None:\n            return recovered\n\n        if snapshot.orders or snapshot.fills:\n            raise AccountBootstrapDenied("BROKER_ACTIVITY_PRESENT")\n        self._preconditions()\n        self._economic_summary(snapshot)\n        identity = self._identity()\n        data = self._data_identity_provider(snapshot)\n        if not isinstance(data, BootstrapDataIdentity):\n            raise AccountBootstrapDenied("DATA_IDENTITY_INVALID")\n'''
new = '''        if snapshot.orders or snapshot.fills:\n            raise AccountBootstrapDenied("BROKER_ACTIVITY_PRESENT")\n        pending = self._pending_bootstrap()\n        self._preconditions(\n            allow_existing_account_file=pending is not None and self._account_path.exists()\n        )\n        self._economic_summary(snapshot)\n        identity = self._identity()\n        data = self._data_identity_provider(snapshot)\n        if not isinstance(data, BootstrapDataIdentity):\n            raise AccountBootstrapDenied("DATA_IDENTITY_INVALID")\n        recovered = self._recover_file_applied(\n            pending,\n            snapshot=snapshot,\n            identity=identity,\n            data=data,\n        )\n        if recovered is not None:\n            return recovered\n'''
if text.count(old) != 1:
    raise SystemExit("bootstrap recovery ordering marker mismatch")
text = text.replace(old, new)

path.write_text(text, encoding="utf-8")
