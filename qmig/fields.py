"""Map test case properties from the source project's fields to the target's.

Field and option IDs differ between qTest instances, so properties are matched
by *label*: field label -> field label, option label -> option label.  Anything
that cannot be matched is dropped and reported, never guessed.
"""
from __future__ import annotations

import re
from typing import Any

# HS fields that HSCS does not get: their values go into the description as migration metadata (see plan.migrated_metadata)
METADATA_FIELDS = ("Eligible to Automate", "Regression Flag", "Estimated Time", "Hierarchy", "Zephyr Test Case ID",
                   "Comments (Archive)")
DEFAULT_SKIP_FIELDS = ("Assigned To",) + METADATA_FIELDS  # Assigned To holds user IDs; users are not migrated yet


def _key(text: Any) -> str:
    return str(text or "").strip().lower()


def _parse_values(raw: Any) -> tuple[list[str], bool]:
    """Return (values, was_multi) from a property value such as 5, "5" or "[5,6]"."""
    if raw is None or raw == "":
        return [], False
    if isinstance(raw, (list, tuple)):
        return [str(v).strip() for v in raw if str(v).strip()], True
    text = str(raw).strip()
    if text.startswith("[") and text.endswith("]"):
        return [v.strip().strip("\"'") for v in text[1:-1].split(",") if v.strip()], True
    return [text], False


class FieldMapper:
    def __init__(self, source_fields: list[dict], target_fields: list[dict],
                 skip_fields: tuple[str, ...] = DEFAULT_SKIP_FIELDS):
        self.skip = {_key(s) for s in skip_fields}
        self.src_by_id = {str(f.get("id")): f for f in source_fields if f.get("id") is not None}
        self.tgt_by_label = {_key(f.get("label")): f for f in target_fields if f.get("label")}
        self.target_fields = target_fields
        self.issues: list[dict] = []
        self._seen: set[tuple] = set()
        self.mapped_labels: set[str] = set()
        self.auto_content_added = 0  # automation test cases that got the default Automation Content
        self._analyse()

    @classmethod
    def disabled(cls) -> "FieldMapper":
        """A mapper that copies no custom field values (for tokens that cannot read the target's field settings)."""
        mapper = cls([], [])
        mapper.copy_properties = False
        mapper._issue("WARN", "fields", "Custom field values are NOT copied (--no-properties); only name, description, "
                                        "precondition and steps are migrated")
        return mapper

    copy_properties = True

    def _issue(self, severity: str, area: str, message: str) -> None:
        marker = (severity, area, message)
        if marker not in self._seen:
            self._seen.add(marker)
            self.issues.append({"severity": severity, "area": area, "message": message})

    # ----------------------------------------------------- static comparison
    @staticmethod
    def _options(field: dict | None) -> dict[str, str]:
        """label(lowercase) -> option value for a field's allowed values."""
        return {_key(o.get("label")): str(o.get("value")) for o in (field or {}).get("allowed_values") or []
                if o.get("label") is not None and o.get("value") is not None}

    def _analyse(self) -> None:
        for field in self.src_by_id.values():
            label = field.get("label") or ""
            if _key(label) in self.skip:
                self._issue("INFO", "fields", f"Field '{label}' is skipped by configuration")
                continue
            target = self.tgt_by_label.get(_key(label))
            if target is None:
                self._issue("WARN", "fields", f"Source field '{label}' does not exist in the target project; its values will be dropped")
                continue
            self.mapped_labels.add(_key(label))
            src_opts, tgt_opts = self._options(field), self._options(target)
            if src_opts and tgt_opts:
                missing = sorted(set(src_opts) - set(tgt_opts))
                if missing:
                    names = ", ".join(f"'{field_opt['label']}'" for field_opt in field.get("allowed_values", [])
                                      if _key(field_opt.get("label")) in missing)
                    self._issue("WARN", "fields", f"Field '{label}': options {names} are missing in the target; those values will be dropped")
            elif bool(src_opts) != bool(tgt_opts):
                self._issue("WARN", "fields", f"Field '{label}' is a list in one project and free-form in the other; values copied as-is")
        for target in self.target_fields:
            if target.get("required") and _key(target.get("label")) not in self.mapped_labels \
                    and _key(target.get("label")) not in self.skip:
                default = next((o for o in target.get("allowed_values") or [] if o.get("is_default")), None)
                hint = f"default '{default.get('label')}' will be used" if default else "test case creation may be rejected"
                self._issue("WARN", "fields", f"Required target field '{target.get('label')}' has no source field; {hint}")

    AUTOMATED_OPTIONS = ("automation", "yes", "tosca", "tosca cloud")
    AUTOMATION_CONTENT_DEFAULT = "auto"

    def _ensure_automation_content(self, out: list[dict]) -> None:
        """qTest refuses an automation test case (Type 'Automation' or Automation Yes/Tosca) without Automation Content
        ('Required Automation Content for Automation test case').  HS allows it, so an empty one gets ``auto``."""
        content = self.tgt_by_label.get("automation content")
        if not content or "automation content" in self.skip or any(p["field_id"] == content["id"] for p in out):
            return
        for label in ("type", "automation"):
            field = self.tgt_by_label.get(label)
            names = {str(o.get("value")): _key(o.get("label")) for o in (field or {}).get("allowed_values") or []}
            for prop in out:
                if field and prop["field_id"] == field["id"] and names.get(str(prop["field_value"])) in self.AUTOMATED_OPTIONS:
                    out.append({"field_id": content["id"], "field_value": self.AUTOMATION_CONTENT_DEFAULT})
                    self.auto_content_added += 1
                    return

    # ------------------------------------------------------ per-test-case map
    def map_properties(self, properties: list[dict]) -> tuple[list[dict], list[str]]:
        """Return (target property list, warnings) for one source test case."""
        if not self.copy_properties:
            return [], []
        out: list[dict] = []
        warnings: list[str] = []
        provided: set[str] = set()
        for prop in properties or []:
            if not isinstance(prop, dict):
                continue
            src_field = self.src_by_id.get(str(prop.get("field_id")), {})
            label = prop.get("field_name") or src_field.get("label") or ""
            if not label or _key(label) in self.skip:
                continue
            target = self.tgt_by_label.get(_key(label))
            if target is None:
                continue  # already reported once at project level
            values, multi = _parse_values(prop.get("field_value"))
            if not values:
                continue
            tgt_opts = self._options(target)
            if tgt_opts:
                src_labels = {str(o.get("value")): o.get("label") for o in src_field.get("allowed_values") or []}
                names = [src_labels.get(v) for v in values] if src_labels else \
                    [n.strip() for n in re.split(r"[;,]", str(prop.get("field_value_name") or "")) if n.strip()]
                mapped: list[str] = []
                for value, name in zip(values, names + [None] * len(values)):
                    option = tgt_opts.get(_key(name)) if name else None
                    if option is None:
                        warnings.append(f"{label}: value '{name or value}' not available in target, dropped")
                    else:
                        mapped.append(option)
                if not mapped:
                    continue
                field_value = f"[{','.join(mapped)}]" if multi else mapped[0]
            else:
                field_value = prop.get("field_value")
            out.append({"field_id": target["id"], "field_value": field_value})
            provided.add(_key(label))
        self._ensure_automation_content(out)
        for target in self.target_fields:
            label = _key(target.get("label"))
            if label in provided or label in self.skip or not target.get("required"):
                continue
            default = next((o for o in target.get("allowed_values") or [] if o.get("is_default")), None)
            if default is not None:
                out.append({"field_id": target["id"], "field_value": str(default.get("value"))})
        return out, warnings
