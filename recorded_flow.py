import asyncio
import re
from playwright.async_api import Playwright, async_playwright, expect


async def run(playwright: Playwright) -> None:
    browser = await playwright.chromium.launch(headless=False)
    context = await browser.new_context()
    page = await context.new_page()
    await page.goto("https://soton.gladstonego.cloud/auth/login")
    await page.get_by_placeholder("Enter your email").click()
    await page.get_by_placeholder("Enter your email").fill("<GYM_USERNAME_FROM_ENV>")
    await page.get_by_placeholder("Enter your email").press("Tab")
    await page.get_by_placeholder("Enter your password").fill("<GYM_PASSWORD_FROM_ENV>")
    await page.get_by_role("button", name="Login", exact=True).click()
    await page.get_by_role("button", name="Make a booking").click()
    await page.get_by_role("textbox", name="What are you looking to do").click()
    await page.get_by_role("textbox", name="What are you looking to do").fill("badminton")
    await page.get_by_role("option", name="Select Badminton option").click()
    await page.get_by_role("button", name="Open calendar").click()
    await page.get_by_text("21", exact=True).click()
    await page.get_by_role("button", name="Search for activities").click()
    await page.get_by_role("button", name="Badminton starts on Thu , 21st May, at 07:00 AM: See available spaces").click()
    await page.get_by_role("button", name="Book now: for Jubilee Court 4 at 2:00 PM Thursday, May 21,").click()
    # Final confirmation button was visible here during recording.
    # Do not click this in recorded_flow.py.
    # await page.get_by_role("button", name="Book Badminton for £0.00 at").click()

    # ---------------------
    await context.close()
    await browser.close()


async def main() -> None:
    async with async_playwright() as playwright:
        await run(playwright)


asyncio.run(main())
