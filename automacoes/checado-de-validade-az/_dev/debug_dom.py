"""One-off: dump the HTML of the first visible row and try opening its popup."""
from __future__ import annotations
import asyncio, os
from pathlib import Path
from dotenv import load_dotenv

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")

from playwright.async_api import async_playwright
from worker import ensure_logged_in, set_page_length_100, TABLE_ID


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await ensure_logged_in(page)
        await set_page_length_100(page)

        # Use DataTables API to get rows on current page
        info = await page.evaluate(f"""
            () => {{
                const dt = $('#{TABLE_ID}').DataTable();
                const nodes = dt.rows({{page: 'current'}}).nodes();
                return {{
                    visible_count: nodes.length,
                    total_tbody_tr: document.querySelectorAll('#{TABLE_ID} tbody tr').length,
                    first_row_html: nodes[0] ? nodes[0].outerHTML : null,
                    page_info: $('#{TABLE_ID}_info').text(),
                }};
            }}
        """)
        print("=== VISIBLE COUNT:", info["visible_count"])
        print("=== TOTAL TBODY TR:", info["total_tbody_tr"])
        print("=== PAGE INFO:", info["page_info"])
        print("=== FIRST ROW HTML ===")
        print(info["first_row_html"])

        # Try to click the manage-tasks icon and see what shows up
        print("\n=== Attempting click on icon... ===")
        first_row = page.locator(
            f"#{TABLE_ID} tbody tr"
        ).filter(has=page.locator('img[title="Gerenciar Tarefas"]')).first
        icon = first_row.locator('img[title="Gerenciar Tarefas"]')
        await icon.scroll_into_view_if_needed()
        # Try clicking the parent (sometimes the click handler is on <a> or <td>)
        parent_tag = await icon.evaluate("el => el.parentElement.tagName")
        print("parent tag of icon:", parent_tag)
        try:
            await icon.click(force=True, timeout=5000)
        except Exception as e:
            print("icon click failed:", e)
        await asyncio.sleep(3)
        # Dump any visible modal/dialog HTML
        modal_html = await page.evaluate("""
            () => {
                const candidates = [...document.querySelectorAll('.modal, [role=dialog], .swal2-container, .swal-overlay, [class*=modal], [class*=Modal]')];
                const visible = candidates.filter(el => el.offsetParent !== null);
                return visible.map(el => ({
                    tag: el.tagName,
                    cls: el.className,
                    snippet: el.outerHTML.slice(0, 600)
                }));
            }
        """)
        print("=== VISIBLE MODALS:", len(modal_html))
        for m in modal_html:
            print(m)
        print("\n=== Page title:", await page.title())
        await browser.close()


asyncio.run(main())
