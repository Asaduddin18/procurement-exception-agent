import { expect, test } from "@playwright/test";

test("uploads, reviews, verifies a duplicate, and resolves a real checkpoint", async ({ page }, info) => {
  await page.goto("/");
  await expect(page.getByText("No purchase orders yet")).toBeVisible();
  const input = page.getByLabel("Upload purchase order PDF");
  const clean = "../seed/sample_pdfs/PO-2026-420_Clean.pdf";
  const exception = "../seed/sample_pdfs/PO-2026-419_Apex_Overcharge.pdf";
  await input.setInputFiles(clean);
  const queue = page.getByRole("table", { name: "Live runs" });
  const cleanRow = queue.getByRole("row").filter({ hasText: "PO-2026-420" });
  await expect(cleanRow.getByText("Approved", { exact: true })).toBeVisible();
  await expect(cleanRow.getByText("$4,200.00")).toBeVisible();

  await input.setInputFiles(exception);
  const exceptionRow = queue.getByRole("row").filter({ hasText: "PO-2026-419" });
  await expect(exceptionRow.getByText("Awaiting approval", { exact: true })).toBeVisible();
  await expect(exceptionRow.getByText("$18,450.00")).toBeVisible();
  await expect(page.locator("tr.flagged").getByText("SS-316 Valve")).toBeVisible();
  await expect(page.getByText("$1,250.00 overcharge · 20.83% above catalog")).toBeVisible();
  await expect(page.getByText("Payment terms mismatch", { exact: true })).toBeVisible();
  await expect(page.getByText(/US-PROCUREMENT v2026 · Section 4.1/)).toBeVisible();
  await page.screenshot({ path: info.outputPath("control-tower-desktop.png"), fullPage: true });

  await page.getByRole("button", { name: "Audit & Trace" }).click();
  await expect(page.getByText("Deterministic risk gate", { exact: true })).toBeVisible();
  await expect(page.getByText("Review required", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Test Duplicate Upload" }).click();
  await expect(page.getByRole("status")).toContainText("The original run was preserved");
  await expect(exceptionRow.getByText("Duplicate blocked")).toBeVisible();
  await expect(queue.getByRole("row")).toHaveCount(3);

  await page.getByRole("button", { name: "Review", exact: true }).click();
  await expect(page.getByRole("button", { name: "Approve Exception" })).toBeDisabled();
  await page.getByLabel("Reviewer notes").fill("Checked the quoted price and payment terms.");
  await page.getByLabel("Reviewer token").fill("ci-procurement-reviewer");
  await page.getByRole("button", { name: "Approve Exception" }).click();
  await expect(page.getByRole("alert")).toContainText("Finance Director approval is required");
  await page.getByRole("button", { name: "Draft Dispute Email" }).click();
  await expect(exceptionRow.getByText("Disputed", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Dispute email draft" })).toBeVisible();
  await expect(page.locator("pre")).toContainText("CTR-APX-2026-01");

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole("heading", { name: "Procurement runs" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.screenshot({ path: info.outputPath("control-tower-mobile.png"), fullPage: true });
});
