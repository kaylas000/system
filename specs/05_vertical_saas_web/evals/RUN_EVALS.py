# specs/05_vertical_saas_web/evals/RUN_EVALS.py
"""
Evaluation Runner for SaaS Web Vertical.
Executes dataset, measures Pass@K, Cost, Latency.
"""

import asyncio
import json
from pathlib import Path
from typing: List, Dict, Any
from kernel.config import settings
from kernel.graph.builder import build_graph
from kernel.vertical.loader import VerticalLoader
from kernel.sandbox.manager import SandboxManager
from kernel.tools.registry import ToolRegistry
from kernel.llm.client import LiteLLMClient

class VerticalEvaluator:
    def __init__(self, vertical_id: str = "saas_web"):
        self.vertical_id = vertical_id
        self.loader = VerticalLoader(Path("verticals"))
        self.vertical = self.loader.load_vertical(vertical_id)
        self.llm = LiteLLMClient()
        self.sandbox_mgr = SandboxManager()
        self.tool_reg = ToolRegistry(self.sandbox_mgr)
        self.graph = build_graph(self.vertical)

    async def run_eval(self, dataset_path: Path, max_concurrent: int = 2) -> Dict[str, Any]:
        with open(dataset_path) as f:
            dataset = [json.loads(line) for line in f]
        
        sem = asyncio.Semaphore(max_concurrent)
        results = []
        
        async def run_one(item):
            async with sem:
                return await self._run_single(item)
        
        tasks = [run_one(item) for item in dataset]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        return self._aggregate(results, dataset)

    async def _run_single(self, item: Dict) -> Dict:
        from kernel.state import AgentState
        from kernel.protocols import GenerateRequest
        
        request = GenerateRequest(
            prompt=item["prompt"],
            vertical_id=self.vertical_id,
            tech_stack_hints=item.get("tech_stack_hints", {}),
            constraints=item.get("constraints", []),
        )
        
        # Execute Graph
        config = {"configurable": {"thread_id": f"eval_{item['id']}", "vertical": self.vertical, "tool_registry": self.tool_reg, "llm_client": self.llm}}
        
        try:
            final_state = await self.graph.ainvoke(None, config=config) # Resume from init
            # Actually need to invoke with input first
            final_state = await self.graph.ainvoke(request.model_dump(), config=config)
            
            success = final_state.get("status") == "completed"
            gates = final_state.get("verification_history", [])
            gate_results = {g["gate_id"]: g["status"] for g in gates}
            
            return {"id": item["id"], "success": success, "gates": gate_results, "cost": final_state.get("token_usage", {}).get("cost_usd", 0), "error": final_state.get("error")}
        except Exception as e:
            return {"id": item["id"], "success": False, "error": str(e)}

    def _aggregate(self, results: List, dataset: List) -> Dict:
        total = len(dataset)
        passed = sum(1 for r in results if isinstance(r, dict) and r.get("success"))
        failed = total - passed
        avg_cost = sum(r.get("cost", 0) for r in results if isinstance(r, dict)) / max(total, 1)
        
        gate_stats = {}
        for r in results:
            if isinstance(r, dict):
                for gate, status in r.get("gates", {}).items():
                    gate_stats.setdefault(gate, {"pass": 0, "fail": 0})
                    gate_stats[gate][status.lower()] += 1
        
        return {
            "vertical": self.vertical_id,
            "total": total, "passed": passed, "failed": failed, "pass_rate": passed/total,
            "avg_cost_usd": avg_cost,
            "gate_stats": gate_stats,
            "details": results
        }

if __name__ == "__main__":
    evaluator = VerticalEvaluator()
    report = asyncio.run(evaluator.run_eval(Path("evals/DATASET.jsonl")))
    print(json.dumps(report, indent=2))
