# specs/03_skills/registry/VERTICAL_LOADER.py
"""
Vertical Loader: Discovers and loads Vertical Plugins.
A Vertical is a directory with:
- manifest.yaml (VerticalManifest)
- skills/ (Skill directories)
- prompts/ (Jinja2 prompt templates)
- rag_collections/ (list of Qdrant collection names)
"""

from __future__ import annotations
import yaml
import importlib.util
from pathlib import Path
from typing: Dict, Optional
from kernel.protocols import IVertical, VerticalManifest

class VerticalLoader:
    def __init__(self, verticals_root: Path):
        self.verticals_root = verticals_root
        self._verticals: Dict[str, IVertical] = {}

    def discover(self) -> Dict[str, VerticalManifest]:
        """Scan verticals_root for manifest.yaml."""
        manifests = {}
        for v_dir in self.verticals_root.iterdir():
            if v_dir.is_dir():
                manifest_path = v_dir / "manifest.yaml"
                if manifest_path.exists():
                    with open(manifest_path) as f:
                        data = yaml.safe_load(f)
                        data["skills_dir"] = str(v_dir / "skills")
                        data["prompts_dir"] = str(v_dir / "prompts")
                        manifests[data["id"]] = VerticalManifest(**data)
        return manifests

    def load_vertical(self, vertical_id: str) -> IVertical:
        if vertical_id in self._verticals:
            return self._verticals[vertical_id]
        
        v_dir = self.verticals_root / vertical_id
        if not v_dir.exists():
            raise ValueError(f"Vertical not found: {vertical_id}")
        
        # 1. Load Manifest
        with open(v_dir / "manifest.yaml") as f:
            manifest_data = yaml.safe_load(f)
        manifest_data["skills_dir"] = str(v_dir / "skills")
        manifest_data["prompts_dir"] = str(v_dir / "prompts")
        manifest = VerticalManifest(**manifest_data)
        
        # 2. Load Custom Implementation (vertical_impl.py) if exists
        impl_path = v_dir / "vertical_impl.py"
        vertical_impl = None
        if impl_path.exists():
            spec = importlib.util.spec_from_file_location(f"vertical_{vertical_id}", impl_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            # Expect a class `VerticalImpl` implementing `IVertical`
            if hasattr(module, "VerticalImpl"):
                vertical_impl = module.VerticalImpl(manifest)
            else:
                raise RuntimeError(f"vertical_impl.py must contain 'VerticalImpl' class")
        else:
            # 3. Default Implementation (GenericVertical)
            from .DEFAULT_VERTICAL import GenericVertical
            vertical_impl = GenericVertical(manifest)
        
        self._verticals[vertical_id] = vertical_impl
        return vertical_impl
