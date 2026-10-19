from __future__ import annotations

from pathlib import Path
import re
from .base import FormQuestion, ProviderResult


FIELD_ALIASES = {
    "name": ("full name", "legal name", "name"),
    "first_name": ("first name",), "last_name": ("last name",),
    "email": ("email", "e-mail"), "phone": ("phone", "telephone"),
    "address": ("address",), "city": ("city",), "state": ("state",), "zip": ("zip", "postal"),
}


def _answer(answers, key):
    aliases = {"name": "identity.full_name", "email": "identity.email", "phone": "identity.phone", "address": "identity.address", "city": "identity.city", "state": "identity.state", "zip": "identity.zip"}
    found = answers.known(aliases.get(key, key)) if answers else None
    return None if found is None else str(found.value)


class GenericProvider:
    name = "generic"
    allow_submit = False

    async def fill(self, page, *, job, resume_path: Path, answers, submit: bool, output_dir: Path) -> ProviderResult:
        unresolved: list[FormQuestion] = []
        filled: list[str] = []
        for key, labels in FIELD_ALIASES.items():
            value = _answer(answers, key)
            if not value:
                continue
            for label in labels:
                try:
                    locator = page.get_by_label(label, exact=False).first
                    if await locator.count():
                        await locator.fill(value)
                        filled.append(key)
                        break
                except Exception:
                    continue
        try:
            uploads = page.locator('input[type="file"]')
            if await uploads.count():
                await uploads.first.set_input_files(str(resume_path))
                filled.append("resume")
        except Exception:
            pass
        # Unknown required controls are surfaced for human input; never guess.
        try:
            required = page.locator("input[required], textarea[required], select[required]")
            for index in range(await required.count()):
                control = required.nth(index)
                try:
                    if await control.is_visible() and not await control.input_value():
                        unresolved.append(FormQuestion(str(index), await control.get_attribute("aria-label") or await control.get_attribute("name") or "Required field", True))
                except Exception:
                    continue
        except Exception:
            return ProviderResult("NEEDS_HUMAN", current_url=getattr(page, "url", ""), details={"reason": "form inspection unavailable"})
        screenshot = output_dir / "browser" / ("pre_submit.png" if submit else "dry_run.png")
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        try:
            await page.screenshot(path=str(screenshot), full_page=True)
        except Exception:
            screenshot = None
        if unresolved:
            return ProviderResult("NEEDS_INPUT", unresolved, current_url=page.url, screenshot=screenshot, details={"filled": filled})
        if not submit:
            return ProviderResult("READY_TO_APPLY", current_url=page.url, screenshot=screenshot, details={"filled": filled})
        if not self.allow_submit:
            return ProviderResult("NEEDS_HUMAN", current_url=page.url, screenshot=screenshot, details={"filled": filled, "reason": "generic provider requires human confirmation"})
        try:
            submit_button = page.get_by_role("button", name=re.compile(r"submit|apply", re.I)).last
            if not await submit_button.count():
                submit_button = page.locator('input[type="submit"], button[type="submit"]').last
            if not await submit_button.count():
                return ProviderResult("NEEDS_HUMAN", current_url=page.url, screenshot=screenshot, details={"filled": filled, "reason": "submit control not found"})
            await submit_button.click()
            await page.wait_for_load_state("domcontentloaded", timeout=10000)
            body = (await page.locator("body").inner_text()).lower()
            markers = ("application submitted", "thank you", "received your application", "successfully applied", "confirmation")
            confirmation = next((marker for marker in markers if marker in body), "")
            if not confirmation:
                return ProviderResult("NEEDS_HUMAN", current_url=page.url, screenshot=screenshot, details={"filled": filled, "reason": "submission confirmation not verified"})
            confirmation_screenshot = output_dir / "browser" / "submission_confirmation.png"
            await page.screenshot(path=str(confirmation_screenshot), full_page=True)
            return ProviderResult("SUBMITTED", current_url=page.url, confirmation_text=confirmation, screenshot=confirmation_screenshot, details={"filled": filled})
        except Exception as exc:
            return ProviderResult("NEEDS_HUMAN", current_url=getattr(page, "url", ""), screenshot=screenshot, details={"filled": filled, "reason": str(exc)})
