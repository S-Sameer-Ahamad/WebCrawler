"""Debug: find the actual heading structure on blog/career pages"""
import asyncio
from playwright.async_api import async_playwright

async def check():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        
        await page.goto("https://silinexglobal.com/blog-details/the-future-of-it-staffing-data-driven-hiring-in-2026", 
                       wait_until="networkidle", timeout=20000)
        await page.wait_for_timeout(3000)
        
        headings = await page.evaluate("""() => {
            return Array.from(document.querySelectorAll('h1, h2, h3')).map(e => ({
                tag: e.tagName,
                text: e.innerText.trim().substring(0, 120),
                cls: e.className.substring(0, 60)
            }));
        }""")
        
        print("=== Blog detail page headings ===")
        for h in headings:
            print(f"  {h['tag']} .{h['cls']} -> {h['text']}")
        
        # Also find any element with "title" class or heading-like text
        likely_titles = await page.evaluate("""() => {
            const sel = '[class*="title"], [class*="heading"], [class*="post-title"], [class*="entry-title"], [class*="article-title"], [class*="page-title"], .blog-title, .article-heading';
            return Array.from(document.querySelectorAll(sel)).slice(0, 5).map(e => ({
                text: e.innerText.trim().substring(0, 120),
                cls: e.className.substring(0, 60),
                tag: e.tagName
            }));
        }""")
        print("\n  Likely title elements:")
        for t in likely_titles:
            print(f"  {t['tag']} .{t['cls']} -> {t['text']}")
        
        # Career detail
        await page.goto("https://silinexglobal.com/career-details/oracle-ebs-finance-functional-consultant",
                       wait_until="networkidle", timeout=20000)
        await page.wait_for_timeout(3000)
        
        headings2 = await page.evaluate("""() => {
            return Array.from(document.querySelectorAll('h1, h2, h3, h4, [class*="title"], [class*="heading"]')).slice(0, 10).map(e => ({
                tag: e.tagName,
                text: e.innerText.trim().substring(0, 120),
                cls: e.className.substring(0, 60)
            }));
        }""")
        print("\n=== Career detail page ===")
        for h in headings2:
            print(f"  {h['tag']} .{h['cls']} -> {h['text']}")
        
        await browser.close()

asyncio.run(check())
