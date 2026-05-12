"""Playwright worker: opens one Page, logs in (if needed), processes a page range."""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Iterable

from playwright.async_api import BrowserContext, Page, TimeoutError as PWTimeout

from parser import Anomaly, is_out_of_target

log = logging.getLogger(__name__)

URL = os.environ["PANORAMA_URL"]
USER = os.environ["PANORAMA_USER"]
PASS = os.environ["PANORAMA_PASS"]
MES_ALVO = os.environ.get("MES_ALVO", "05")
ANO_ALVO = os.environ.get("ANO_ALVO", "2026")

LENGTH_SELECT = 'select[name="DataTables_Aberto_0_length"]'
TABLE_ID = "DataTables_Aberto_0"


async def _safe_goto(page: Page, url: str) -> None:
    """goto with retries — the app sometimes redirects mid-load and triggers ERR_ABORTED."""
    for attempt in range(3):
        try:
            await page.goto(url, wait_until="load", timeout=30000)
            return
        except Exception as exc:
            log.warning("goto attempt %d failed: %s", attempt + 1, exc)
            await asyncio.sleep(1.0)
    # Final attempt — let any error propagate.
    await page.goto(url, wait_until="commit", timeout=30000)


async def ensure_logged_in(page: Page) -> None:
    """Navigate to the app and authenticate if a login form shows up."""
    await _safe_goto(page, URL)
    pwd = page.locator('input[type="password"]').first
    try:
        await pwd.wait_for(state="visible", timeout=4000)
    except PWTimeout:
        return
    user_field = page.locator(
        'input[name="login"], input[name="usuario"], input[name="user"], '
        'input[name="username"], input[name="email"], input[type="text"]'
    ).first
    await user_field.fill(USER)
    await pwd.fill(PASS)
    submit = page.locator(
        'button[type="submit"], input[type="submit"], '
        'button:has-text("Entrar"), button:has-text("Acessar")'
    ).first
    try:
        await submit.click()
    except Exception:
        await pwd.press("Enter")
    await page.wait_for_load_state("networkidle")


async def set_page_length_100(page: Page) -> None:
    await page.wait_for_selector(LENGTH_SELECT, state="visible", timeout=30000)
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await page.select_option(LENGTH_SELECT, "100")
    # Wait until DataTables redraws — either 100 rows on page, or fewer if total < 100.
    await page.wait_for_function(
        f"""() => {{
            const dt = $('#{TABLE_ID}').DataTable();
            const onPage = dt.rows({{page:'current'}}).nodes().length;
            const info = dt.page.info();
            return onPage === 100 || (info.recordsDisplay < 100 && onPage === info.recordsDisplay);
        }}""",
        timeout=20000,
    )


async def discover_total_pages(page: Page) -> int:
    info = await page.evaluate(f"() => $('#{TABLE_ID}').DataTable().page.info()")
    log.info("DataTables info: pages=%s recordsTotal=%s recordsDisplay=%s",
             info.get("pages"), info.get("recordsTotal"), info.get("recordsDisplay"))
    return int(info["pages"])


async def goto_page(page: Page, page_num: int) -> None:
    await page.evaluate(
        f"(n) => $('#{TABLE_ID}').DataTable().page(n - 1).draw('page')",
        page_num,
    )
    await page.wait_for_function(
        f"(n) => $('#{TABLE_ID}').DataTable().page() === n - 1",
        arg=page_num,
        timeout=15000,
    )
    await asyncio.sleep(0.2)


async def list_current_page_rows(page: Page) -> list[dict]:
    """Return [{empresa, empresa_id}, ...] for rows on the current DataTables page."""
    return await page.evaluate(f"""
        () => {{
            const nodes = $('#{TABLE_ID}').DataTable().rows({{page:'current'}}).nodes();
            const out = [];
            for (let i = 0; i < nodes.length; i++) {{
                const tr = nodes[i];
                const empresa = (tr.querySelector('td a.mon-empresa-link')?.textContent || tr.querySelector('td')?.textContent || '').trim();
                const trigger = tr.querySelector('a[onclick*="abrirDialogD"]');
                let empresaId = null;
                if (trigger) {{
                    const m = trigger.getAttribute('onclick').match(/abrirDialogD\\('(\\d+)'\\)/);
                    if (m) empresaId = m[1];
                }}
                out.push({{ empresa, empresa_id: empresaId }});
            }}
            return out;
        }}
    """)


async def open_modal_and_read(page: Page, empresa_id: str) -> list[tuple[str, str]]:
    """Click the trigger anchor, wait for AJAX-loaded tbody, return [(tarefa, data), ...].

    Calling abrirDialogD() via evaluate alone does NOT open the Bootstrap modal —
    the modal show is bound to the click event of the <a data-toggle="modal">.
    We trigger jQuery's .click() so Bootstrap's delegated handler fires + the
    inline onclick=abrirDialogD() runs (which kicks the AJAX).
    """
    modal_sel = f"#modal-empresa-{empresa_id}-D"
    tbody_sel = f"#tbody-empresa-{empresa_id}-D"

    async def _try_open() -> bool:
        await page.evaluate(f"""
            () => {{
                const a = document.querySelector('a[data-target="{modal_sel}"]');
                if (a) {{ $(a).click(); }}
                else {{ abrirDialogD('{empresa_id}'); $('{modal_sel}').modal('show'); }}
            }}
        """)
        try:
            # Each task row is a mix of <th> (checkbox, empresa) and <td>
            # (tarefa, situação, confirmação). Count all cells via tr.children.
            await page.wait_for_function(
                f"""() => {{
                    const m = document.querySelector('{modal_sel}');
                    if (!m || !m.classList.contains('show')) return false;
                    const tb = document.querySelector('{tbody_sel}');
                    if (!tb) return false;
                    const trs = [...tb.querySelectorAll('tr')];
                    if (!trs.length) return false;
                    return trs.some(tr => {{
                        const cells = tr.children;
                        if (cells.length >= 4) return true;
                        // "No data" row pattern.
                        if (cells.length === 1 && cells[0].hasAttribute('colspan')) return true;
                        return false;
                    }});
                }}""",
                timeout=15000,
            )
            return True
        except PWTimeout:
            return False

    if not await _try_open():
        # Retry once after a short pause — first load can be slow on cold cache.
        log.warning("retrying modal for empresa_id=%s", empresa_id)
        await close_modal(page, empresa_id)
        await asyncio.sleep(1.0)
        if not await _try_open():
            raise PWTimeout(f"modal for empresa_id={empresa_id} never loaded after retry")

    result = await page.evaluate(f"""
        () => {{
            const tb = document.querySelector('{tbody_sel}');
            if (!tb) return {{ rows: [], html_snippet: null }};
            const trs = [...tb.querySelectorAll('tr')];
            const rows = trs.map(tr => {{
                const cells = [...tr.children];
                if (cells.length < 4) return null;
                // Layout: [checkbox(th), empresa(th), tarefa(td), situacao(td), confirmacao(td)]
                // Pick from the END so we work whether the checkbox/empresa columns exist or not.
                return {{
                    tarefa: cells[cells.length - 3].innerText.trim(),
                    data: cells[cells.length - 1].innerText.trim(),
                }};
            }}).filter(Boolean);
            return {{
                rows,
                html_snippet: rows.length === 0 ? tb.innerHTML.slice(0, 400) : null,
            }};
        }}
    """)
    if not result["rows"] and result["html_snippet"]:
        log.warning("empty popup for empresa_id=%s, tbody HTML: %s", empresa_id, result["html_snippet"])
    return [(r["tarefa"], r["data"]) for r in result["rows"]]


async def close_modal(page: Page, empresa_id: str) -> None:
    """Close via jQuery .modal('hide') — more reliable than clicking the button.

    Bootstrap's hide is async (fade animation). We check the modal isn't
    visible via :visible (display !== 'none') rather than the .show class,
    since custom themes can lock animation states.
    """
    modal_sel = f"#modal-empresa-{empresa_id}-D"
    try:
        await page.evaluate(f"""
            () => {{
                // Hide every open modal — defensive against stuck state.
                $('.modal.show, .modal:visible').modal('hide');
                $('{modal_sel}').modal('hide');
            }}
        """)
    except Exception as exc:
        log.debug("modal hide JS failed: %s", exc)
    try:
        await page.wait_for_function(
            f"""() => {{
                const m = document.querySelector('{modal_sel}');
                if (!m) return true;
                const style = window.getComputedStyle(m);
                if (style.display === 'none') return true;
                return !document.body.classList.contains('modal-open')
                       && !document.querySelector('.modal-backdrop');
            }}""",
            timeout=3000,
        )
    except PWTimeout:
        # Last resort — force-hide via DOM manipulation and clear backdrop.
        await page.evaluate(f"""
            () => {{
                document.querySelectorAll('.modal').forEach(m => {{
                    m.classList.remove('show');
                    m.style.display = 'none';
                }});
                document.body.classList.remove('modal-open');
                document.querySelectorAll('.modal-backdrop').forEach(b => b.remove());
            }}
        """)


async def collect_page_anomalies(page: Page, page_num: int, bot_id: int) -> list[Anomaly]:
    anomalies: list[Anomaly] = []
    rows = await list_current_page_rows(page)
    log.info("bot=%d page=%d rows=%d", bot_id, page_num, len(rows))

    for idx, row in enumerate(rows):
        empresa = row["empresa"]
        empresa_id = row["empresa_id"]
        if not empresa_id:
            log.warning("bot=%d page=%d row=%d empresa=%r — no empresa_id, skipping", bot_id, page_num, idx, empresa)
            continue
        try:
            tarefas = await open_modal_and_read(page, empresa_id)
        except PWTimeout:
            log.warning("bot=%d page=%d empresa=%r popup never loaded", bot_id, page_num, empresa)
            await close_modal(page, empresa_id)
            continue
        except Exception as exc:
            log.warning("bot=%d page=%d empresa=%r error: %s", bot_id, page_num, empresa, exc)
            await close_modal(page, empresa_id)
            continue

        n_anom_before = len(anomalies)
        for tarefa, data in tarefas:
            if is_out_of_target(data, MES_ALVO, ANO_ALVO):
                anomalies.append(Anomaly(empresa, tarefa, data, page_num, bot_id))
        log.info(
            "bot=%d page=%d empresa=%r tarefas=%d anomalias=+%d",
            bot_id, page_num, empresa[:60], len(tarefas), len(anomalies) - n_anom_before,
        )

        await close_modal(page, empresa_id)
        await asyncio.sleep(0.05)

    return anomalies


async def run_worker(
    context: BrowserContext,
    bot_id: int,
    pages: Iterable[int],
) -> list[Anomaly]:
    page = await context.new_page()
    await ensure_logged_in(page)
    await set_page_length_100(page)

    found: list[Anomaly] = []
    for p in pages:
        try:
            await goto_page(page, p)
            found.extend(await collect_page_anomalies(page, p, bot_id))
        except Exception:
            log.exception("bot=%d page=%d failed", bot_id, p)
    await page.close()
    return found
