from __future__ import annotations

from pathlib import Path
import re
import asyncio
from typing import Any

try:  # Playwright is the optional "apply" extra; only needed when a form is actually driven.
    from playwright.async_api import expect
except ImportError:  # pragma: no cover - exercised only without the extra installed
    def expect(*_args, **_kwargs):
        raise RuntimeError("Browser automation needs the optional extra: pip install -e '.[apply]' && playwright install chromium")

from .base import FormQuestion, ProviderResult
from .generic import GenericProvider


class GreenhouseProvider(GenericProvider):
    """Greenhouse filler with exact labels and verified React control state."""

    name = "greenhouse"
    allow_submit = True

    @staticmethod
    def _answer(answers: Any, key: str) -> str | None:
        found = answers.known(key) if answers else None
        return None if found is None else str(found.value)

    @staticmethod
    def _label(page, label: str):
        exact = page.get_by_label(label, exact=True).first
        # Greenhouse sometimes leaves a trailing space before the required
        # marker (notably the season question). Keep semantic lookup primary,
        # then tolerate only label-whitespace differences.
        return exact

    async def _field(self, page, label: str):
        field = self._label(page, label)
        if await field.count():
            return field
        # Astranis' Greenhouse React Select controls expose their label via
        # aria-labelledby, but Playwright's label association is not reliable
        # for this rendered structure. These IDs were inspected from the live
        # form and are limited to the two controls being repaired.
        inspected_ids = {
            "Country": "#country",
            "Location (City)": "#candidate-location",
        }
        selector = inspected_ids.get(label)
        if selector:
            inspected = page.locator(selector).first
            if await inspected.count():
                return inspected
        return page.get_by_label(re.compile(r"^\s*" + re.escape(label) + r"\s*$", re.I)).first

    async def _text(self, page, label: str, value: str, *, required: bool = True) -> bool:
        field = await self._field(page, label)
        if await field.count() == 0:
            if required:
                raise LookupError(f"Greenhouse field not found: {label}")
            return False
        await expect(field).to_be_visible()
        await field.fill(value)
        await expect(field).to_have_value(value)
        await expect(await self._field(page, label)).to_have_value(value)
        return True

    async def _combobox(self, page, label: str, value: str, *, option_pattern: str | None = None) -> bool:
        field = await self._field(page, label)
        if await field.count() == 0:
            raise LookupError(f"Greenhouse combobox not found: {label}")
        await expect(field).to_be_visible()
        await field.click()
        try:
            await field.fill(value)
        except Exception:
            pass
        pattern = option_pattern or re.escape(value)
        options = page.locator('[role="option"]:visible').filter(has_text=re.compile(pattern, re.I))
        if await options.count() == 0:
            options = page.locator('.select__option:visible').filter(has_text=re.compile(pattern, re.I))
        if await options.count() == 0:
            # This Greenhouse build renders the location menu in a portal
            # without role=option. Keep the fallback constrained to visible
            # text matching the already inspected city value.
            options = page.get_by_text(re.compile(pattern, re.I), exact=False).filter(visible=True)
        if await options.count() == 0:
            # The live Astranis location portal can expose no semantic option
            # nodes. Use the permitted keyboard fallback, then verify the
            # rendered selection below; never treat typing alone as success.
            if label == "Location (City)":
                await field.press("ArrowDown")
                await field.press("Enter")
                await page.wait_for_timeout(250)
                container = field.locator("xpath=ancestor::*[contains(@class, 'select__container')][1]")
                visible_text = (await container.inner_text()).strip()
                if "san jose" in visible_text.lower() and await field.get_attribute("aria-expanded") == "false":
                    return True
            raise LookupError(f"No selectable Greenhouse option for {label}: {value}")
        await options.last.click()
        await page.wait_for_timeout(150)
        container = field.locator("xpath=ancestor::*[contains(@class, 'select__container')][1]")
        visible_text = (await container.inner_text()).strip()
        hidden = container.locator("xpath=..").locator('input[required][aria-hidden="true"]')
        hidden_value = await hidden.first.input_value() if await hidden.count() else ""
        expanded = await field.get_attribute("aria-expanded")
        selected = value.lower() in visible_text.lower() or bool(hidden_value)
        # This Greenhouse Country control renders the selected U.S. entry as
        # its dialing code (+1), not the country name, and has no hidden value.
        if label == "Country" and value.lower() == "united states":
            selected = "+1" in visible_text and expanded == "false"
        if not selected:
            raise AssertionError(f"Greenhouse selection not verified for {label}: {visible_text!r}")
        return True

    async def _pause_for_location(self, output_dir: Path) -> None:
        """Pause in this live page until the operator signals continuation."""
        root = output_dir.parents[1]
        data = root / "data"
        continue_signal = data / "browser-session-continue"
        close_signal = data / "browser-session-close"
        data.mkdir(parents=True, exist_ok=True)
        continue_signal.unlink(missing_ok=True)
        close_signal.unlink(missing_ok=True)
        print(
            "MANUAL ACTION REQUIRED: select your location in the Location (City) field, "
            "then run: jobbeast browser-session <job_id> --continue",
            flush=True,
        )
        while not continue_signal.exists() and not close_signal.exists():
            await asyncio.sleep(1)
        if close_signal.exists():
            raise LookupError("Manual browser session close requested")
        continue_signal.unlink(missing_ok=True)
        await asyncio.sleep(0.5)

    async def _verify_location(self, page) -> None:
        field = await self._field(page, "Location (City)")
        if await field.count() == 0:
            raise LookupError("Greenhouse Location (City) control disappeared after manual selection")
        container = field.locator("xpath=ancestor::*[contains(@class, 'select__container')][1]")
        visible_text = (await container.inner_text()).strip()
        expanded = await field.get_attribute("aria-expanded")
        if "san jose" not in visible_text.lower() or expanded == "true":
            raise AssertionError(f"Manual Location selection not persisted: {visible_text!r}")

    async def _required_questions(self, page) -> list[FormQuestion]:
        unresolved: list[FormQuestion] = []
        seen: set[str] = set()
        controls = page.locator('[aria-required="true"], input[required], textarea[required], select[required]')
        for index in range(await controls.count()):
            control = controls.nth(index)
            try:
                if not await control.is_visible():
                    continue
                ident = await control.get_attribute("id") or await control.get_attribute("name") or str(index)
                if ident in seen:
                    continue
                role = await control.get_attribute("role")
                if role == "combobox":
                    container = control.locator("xpath=ancestor::*[contains(@class, 'select__container')][1]")
                    if (await container.inner_text()).strip():
                        continue
                    label = await control.get_attribute("aria-label") or ident
                elif (await control.input_value()).strip():
                    continue
                else:
                    label = await control.get_attribute("aria-label") or await control.get_attribute("name") or ident
                seen.add(ident)
                unresolved.append(FormQuestion(ident, label, True, "combobox" if role == "combobox" else "text"))
            except Exception:
                continue
        return unresolved

    async def fill(self, page, *, job, resume_path: Path, answers, submit: bool, output_dir: Path) -> ProviderResult:
        filled: list[str] = []
        try:
            full_name = self._answer(answers, "identity.full_name") or ""
            first = self._answer(answers, "first_name") or full_name.split(" ", 1)[0]
            preferred = self._answer(answers, "preferred_name")
            last = self._answer(answers, "last_name") or (full_name.split(" ", 1)[1] if " " in full_name else "")
            standard = (
                ("First Name", first, True),
                ("Preferred First Name", preferred, False),
                ("Last Name", last, True),
                ("Email", self._answer(answers, "identity.email"), True),
                ("Phone", self._answer(answers, "identity.phone"), True),
            )
            for label, value, required in standard:
                if value and await self._text(page, label, value, required=required):
                    filled.append(label)

            country = self._answer(answers, "country") or self._answer(answers, "identity.country")
            location = self._answer(answers, "candidate_location")
            if country:
                await self._combobox(page, "Country", country, option_pattern=r"^United States")
                filled.append("Country")
            if location:
                try:
                    await self._combobox(
                        page,
                        "Location (City)",
                        location,
                        option_pattern=rf"^{re.escape(location)}$",
                    )
                except LookupError:
                    await self._pause_for_location(output_dir)
                    await self._verify_location(page)
                filled.append("Location (City)")

            # Greenhouse renders Resume/CV before Cover Letter; use the
            # inspected semantic id so the first generic file input cannot be
            # mistaken for the wrong attachment.
            upload = page.locator('#resume').first
            if await upload.count() == 0:
                upload = page.locator('input[type="file"]').first
            if await upload.count() == 0:
                raise LookupError("Greenhouse resume upload control not found")
            await upload.set_input_files(str(resume_path))
            filename = await upload.evaluate("el => el.files && el.files[0] ? el.files[0].name : ''")
            if filename != resume_path.name:
                raise AssertionError(f"Resume upload not verified: {filename!r}")
            resume_group = page.locator('[aria-labelledby="upload-label-resume"]')
            resume_text = (await resume_group.inner_text()).strip() if await resume_group.count() else ""
            if resume_path.name not in resume_text and filename != resume_path.name:
                raise AssertionError(f"Resume UI upload not verified: {resume_text!r}")
            filled.append("Resume/CV")

            specific = (
                ("Astranis complies with U.S. Government space technology export regulations, therefore will you state which of the following applies to you:", "export_control_eligibility.answer", True),
                ("What is the most impressive thing you have ever accomplished?", "impressive_accomplishment", False),
                ("Please confirm the season you are applying for.", "season", True),
                ("When are you able to join Astranis as an intern? (12 week minimum)", "start_availability", False),
                ("When do you plan on ending your internship?", "end_availability", False),
                ("At Astranis, we value in-person collaboration and a strong work ethic. Are you comfortable with working onsite at our San Francisco HQ 5 days a week and commit to 55 hours per week?", "onsite_5_days_55_hours", True),
                ("How did you hear about Astranis?", "referral_source", False),
                ("By selecting YES, I consent to receive recruiting SMS messages from Astranis at the phone number provided on my job application.", "recruiting_sms_consent", True),
            )
            for label, key, is_combo in specific:
                value = self._answer(answers, key)
                if value is None:
                    continue
                if is_combo:
                    await self._combobox(page, label, value, option_pattern=re.escape(value.rstrip(".")))
                else:
                    await self._text(page, label, value)
                filled.append(label)
        except (LookupError, AssertionError) as exc:
            unresolved = await self._required_questions(page)
            shot = output_dir / "browser" / ("pre_submit.png" if submit else "dry_run.png")
            shot.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(shot), full_page=True)
            return ProviderResult("NEEDS_INPUT", unresolved or [FormQuestion("provider", str(exc), True)], current_url=page.url, screenshot=shot, details={"filled": filled, "reason": str(exc)})

        unresolved = await self._required_questions(page)
        shot = output_dir / "browser" / ("pre_submit.png" if submit else "dry_run.png")
        shot.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(shot), full_page=True)
        if unresolved:
            return ProviderResult("NEEDS_INPUT", unresolved, current_url=page.url, screenshot=shot, details={"filled": filled})
        if not submit:
            return ProviderResult("READY_TO_APPLY", current_url=page.url, screenshot=shot, details={"filled": filled})
        try:
            button = page.get_by_role("button", name=re.compile(r"submit application", re.I)).last
            await expect(button).to_be_visible()
            await button.click()
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
            body = (await page.locator("body").inner_text()).lower()
            confirmation = next((marker for marker in ("application submitted", "thank you", "received your application", "successfully applied", "confirmation") if marker in body), "")
            if not confirmation:
                return ProviderResult("NEEDS_HUMAN", current_url=page.url, screenshot=shot, details={"filled": filled, "reason": "submission confirmation not verified"})
            confirmation_shot = output_dir / "browser" / "submission_confirmation.png"
            await page.screenshot(path=str(confirmation_shot), full_page=True)
            return ProviderResult("SUBMITTED", current_url=page.url, confirmation_text=confirmation, screenshot=confirmation_shot, details={"filled": filled})
        except Exception as exc:
            return ProviderResult("NEEDS_HUMAN", current_url=page.url, screenshot=shot, details={"filled": filled, "reason": str(exc)})
