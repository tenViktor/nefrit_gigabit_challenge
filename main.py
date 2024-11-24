import json
import asyncio
import pandas as pd
from pathlib import Path
import typer
from rich.console import Console
from rich.progress import track
from playwright.async_api import (
    async_playwright,
    TimeoutError as PlaywrightTimeoutError,
)
from vulnerability_classifier import classify_vulnerability
from script_generator import ScriptGenerator

app = typer.Typer()
console = Console()


class VulnerabilityScanner:
    def __init__(self, target_url: str, timeout: int = 5000):
        self.target_url = target_url
        self.timeout = timeout
        self.results_dir = Path("results")
        self.results_dir.mkdir(exist_ok=True)

        # Initialize script generator
        self.script_generator = ScriptGenerator(self.results_dir)

    async def check_site_availability(self) -> tuple[bool, str]:
        console.print(f"Checking if {self.target_url} is accessible...", style="yellow")
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                page = await browser.new_page()
                await page.goto(self.target_url, timeout=self.timeout)
                await browser.close()
                console.print("\n✅ Target site is accessible!", style="green")
                return True, ""
            except PlaywrightTimeoutError:
                error_msg = (
                    f"\n❌ Error: Cannot access {self.target_url}\n"
                    "Please make sure the Docker container is running and the site is accessible."
                )
                await browser.close()
                return False, error_msg
            except Exception as e:
                error_msg = f"\n❌ Error checking site availability: {str(e)}"
                await browser.close()
                return False, error_msg

    async def save_results(self, vulnerability: str, results: dict):
        """
        Save test results in JSON format with metadata
        """
        timestamp = pd.Timestamp.now().strftime("%Y%m%d_%H%M%S")
        result_data = {
            "timestamp": timestamp,
            "vulnerability": vulnerability,
            "target_url": self.target_url,
            "results": results,
        }

        # Save JSON results
        output_file = (
            self.results_dir / f"{timestamp}_{vulnerability.replace(' ', '_')}.json"
        )
        with open(output_file, "w") as f:
            json.dump(result_data, f, indent=2)

        console.print(f"Results saved to {output_file}", style="green")

    async def run_vulnerability_test(self, vulnerability: str, details: str):
        vuln_type, can_use_playwright = classify_vulnerability(vulnerability, details)
        console.print(f"\nClassified as: {vuln_type.value}", style="blue")

        results = None

        try:
            if can_use_playwright:
                console.print(
                    "Generating and running Playwright test...", style="yellow"
                )
                script = await self.script_generator.generate_test_script(
                    vulnerability, details, vuln_type.value
                )
                results = await self.run_generated_script(script, vulnerability)
            else:
                console.print("Manual testing required", style="red")
                results = {
                    "success": False,
                    "status": "manual_testing_required",
                    "reason": "No automated test available",
                }

            # Save results
            await self.save_results(vulnerability, results)

            # Display results in CLI
            if results.get("success"):
                console.print("✅ Vulnerability CONFIRMED!", style="green")
                if results.get("evidence"):
                    console.print("\nEvidence:", style="yellow")
                    for evidence in results["evidence"]:
                        console.print(f"- {evidence}")
            else:
                console.print("❌ Vulnerability not confirmed", style="red bold")

            if results.get("screenshots"):
                console.print(
                    f"\nScreenshots saved: {len(results['screenshots'])}", style="blue"
                )

        except Exception as e:
            console.print(f"Error during test execution: {str(e)}", style="red")
            results = {"status": "error", "error": str(e)}
            await self.save_results(vulnerability, results)

    async def run_generated_script(self, script_content: str, vulnerability: str):
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()

            try:
                # Create namespace for script execution
                namespace = {
                    "page": page,
                    "TARGET_URL": self.target_url,
                    "Path": Path,
                    "pd": pd,
                    "results_dir": self.results_dir,
                    "__builtins__": __builtins__,
                }

                # Compile and execute the script
                compiled_code = compile(script_content, "<string>", "exec")
                exec(compiled_code, namespace)

                if "main" in namespace and callable(namespace["main"]):
                    result = namespace["main"]()
                    if asyncio.iscoroutine(result):
                        return await result
                    console.print(
                        "Warning: main() function is not async", style="yellow"
                    )
                    return {
                        "success": False,
                        "error": "Script main() function is not async",
                    }
                else:
                    console.print("Warning: No main() function found", style="yellow")
                    return {
                        "success": False,
                        "error": "No main() function in generated script",
                    }

            except Exception as e:
                console.print(f"Error executing script: {str(e)}", style="red")
                return {"success": False, "error": str(e)}
            finally:
                await context.close()
                await browser.close()


@app.command()
def scan(
    target_url: str = typer.Argument(..., help="Target URL to scan"),
    csv_path: str = typer.Argument(..., help="Path to vulnerabilities CSV file"),
):
    """
    Run automated vulnerability scanning based on CSV input
    """

    async def run_scan():
        scanner = VulnerabilityScanner(target_url)

        is_available, error_msg = await scanner.check_site_availability()
        if not is_available:
            console.print(error_msg, style="red")
            return  # Exit gracefully without raising an exception

        df = pd.read_csv(csv_path)
        console.print(f"\nFound {len(df)} vulnerabilities to test", style="blue")

        for _, row in track(
            df.iterrows(), total=len(df), description="Running tests..."
        ):
            await scanner.run_vulnerability_test(
                str(row["Vulnerability"]), str(row["Details"])
            )

    asyncio.run(run_scan())


if __name__ == "__main__":
    app()
