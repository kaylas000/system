# specs/04_knowledge/storage/KUZU_CLIENT.py
"""
Kuzu Graph DB Client (Embedded OLAP Graph).
Used for Code Graph: Call Graph, Import Graph, Inheritance.
"""

import kuzu
from typing: List, Dict, Any, Tuple
from ..ingestion.CHUNKING_STRATEGIES import RetrievalChunk

class KuzuGraph:
    def __init__(self, db_path: str = "./kuzu_db"):
        self.db = kuzu.Database(db_path)
        self.conn = kuzu.Connection(self.db)
        self._init_schema()

    def _init_schema(self):
        # Node Tables
        self.conn.execute("CREATE NODE TABLE IF NOT EXISTS Repo (name STRING, url STRING, branch STRING, framework_version STRING, PRIMARY KEY (name))")
        self.conn.execute("CREATE NODE TABLE IF NOT EXISTS File (path STRING, language STRING, hash STRING, PRIMARY KEY (path))")
        self.conn.execute("""
            CREATE NODE TABLE IF NOT EXISTS Symbol (
                id STRING, name STRING, type STRING, signature STRING, 
                start_line INT64, end_line INT64, intent STRING, pattern STRING, 
                complexity STRING, tags STRING, PRIMARY KEY (id)
            )
        """)
        self.conn.execute("CREATE NODE TABLE IF NOT EXISTS Config (path STRING, type STRING, hash STRING, PRIMARY KEY (path))")
        
        # Rel Tables
        self.conn.execute("CREATE REL TABLE IF NOT EXISTS CONTAINS (FROM Repo TO File, FROM File TO Symbol, FROM File TO Config)")
        self.conn.execute("CREATE REL TABLE IF NOT EXISTS IMPORTS (FROM Symbol TO Symbol, import_type STRING)")
        self.conn.execute("CREATE REL TABLE IF NOT EXISTS CALLS (FROM Symbol TO Symbol, call_type STRING, line INT64)")
        self.conn.execute("CREATE REL TABLE IF NOT EXISTS INHERITS (FROM Symbol TO Symbol)")
        self.conn.execute("CREATE REL TABLE IF NOT EXISTS IMPLEMENTS (FROM Symbol TO Symbol)")
        self.conn.execute("CREATE REL TABLE IF NOT EXISTS USES_TYPE (FROM Symbol TO Symbol, context STRING)")

    def upsert_chunks(self, chunks: List[RetrievalChunk]):
        """Batch upsert nodes and edges from chunks."""
        # 1. Collect unique nodes
        repos = {}
        files = {}
        symbols = {}
        configs = {}
        edges = [] # (src_id, rel, dst_id, props)
        
        for chunk in chunks:
            meta = chunk.metadata
            repo_name = meta["repo"]
            file_path = meta["file_path"]
            
            # Repo
            if repo_name not in repos:
                repos[repo_name] = {"name": repo_name, "url": meta.get("repo_url"), "branch": meta.get("branch"), "framework_version": meta.get("framework_version")}
            
            # File
            if file_path not in files:
                files[file_path] = {"path": file_path, "language": meta.get("language"), "hash": meta.get("content_hash", "")}
            
            # Symbol / Config
            if meta.get("chunk_type") == "symbol":
                sym_id = chunk.id
                if sym_id not in symbols:
                    symbols[sym_id] = {
                        "id": sym_id, "name": meta.get("symbol_name"), "type": meta.get("symbol_type"),
                        "signature": meta.get("signature", "")[:500], "start_line": meta.get("start_line"),
                        "end_line": meta.get("end_line"), "intent": meta.get("intent", ""),
                        "pattern": meta.get("pattern", ""), "complexity": meta.get("complexity", ""),
                        "tags": ",".join(meta.get("tags", []))
                    }
            elif meta.get("chunk_type") == "config":
                if file_path not in configs:
                    configs[file_path] = {"path": file_path, "type": Path(file_path).name, "hash": meta.get("content_hash", "")}
            
            # Edges from chunk.graph_edges
            edges.extend(chunk.graph_edges)
        
        # 2. Execute COPY FROM or INSERT (Kuzu supports COPY FROM CSV/Arrow for speed)
        # For spec, we use individual inserts (slow but clear). Production: Use Pandas/Arrow -> COPY.
        
        for repo in repos.values():
            self.conn.execute("MERGE (r:Repo {name: $name, url: $url, branch: $branch, framework_version: $fw})", repo)
        
        for file in files.values():
            self.conn.execute("MERGE (f:File {path: $path, language: $lang, hash: $hash})", file)
            self.conn.execute("MATCH (r:Repo {name: $repo}), (f:File {path: $path}) MERGE (r)-[:CONTAINS]->(f)", 
                              {"repo": repo_name, "path": file["path"]}) # Need repo_name context
            
        for sym in symbols.values():
            self.conn.execute("""
                MERGE (s:Symbol {id: $id}) 
                SET s.name=$name, s.type=$type, s.signature=$sig, s.start_line=$sl, s.end_line=$el, 
                    s.intent=$intent, s.pattern=$pattern, s.complexity=$comp, s.tags=$tags
            """, sym)
            # Link File -> Symbol
            self.conn.execute("MATCH (f:File {path: $fpath}), (s:Symbol {id: $sid}) MERGE (f)-[:CONTAINS]->(s)",
                              {"fpath": meta["file_path"], "sid": sym["id"]}) # Need file_path context
            
        for config in configs.values():
            self.conn.execute("MERGE (c:Config {path: $path, type: $type, hash: $hash})", config)
            self.conn.execute("MATCH (f:File {path: $path}), (c:Config {path: $path}) MERGE (f)-[:CONTAINS]->(c)", config)
            
        # 3. Edges (Batch via COPY ideally)
        for src, rel, dst, *props in edges:
            prop_dict = props[0] if props else {}
            if rel == "IMPORTS":
                self.conn.execute("MATCH (a:Symbol {id: $src}), (b:Symbol {id: $dst}) MERGE (a)-[:IMPORTS {import_type: $it}]->(b)", 
                                  {"src": src, "dst": dst, "it": prop_dict.get("import_type", "named")})
            elif rel == "CALLS":
                self.conn.execute("MATCH (a:Symbol {id: $src}), (b:Symbol {id: $dst}) MERGE (a)-[:CALLS {call_type: $ct, line: $ln}]->(b)",
                                  {"src": src, "dst": dst, "ct": prop_dict.get("call_type", "static"), "ln": prop_dict.get("line", 0)})
            # ... INHERITS, IMPLEMENTS, USES_TYPE

    def query_callers(self, symbol_name: str, file_path: str = "", limit: int = 20) -> List[Dict]:
        where = "callee.name = $name"
        params = {"name": symbol_name, "limit": limit}
        if file_path:
            where += " AND callee.file_path = $fpath"
            params["fpath"] = file_path
            
        query = f"""
        MATCH (caller:Symbol)-[:CALLS]->(callee:Symbol)
        WHERE {where}
        RETURN caller.id, caller.name, caller.file_path, caller.intent, caller.pattern, caller.signature
        LIMIT $limit
        """
        return self.conn.execute(query, params).get_as_df().to_dict("records")

    def query_dependencies(self, file_path: str, depth: int = 1) -> List[Dict]:
        query = f"""
        MATCH (f:File {{path: $path}})-[:CONTAINS]->(s:Symbol)-[:IMPORTS|CALLS*1..{depth}]->(dep:Symbol)
        RETURN DISTINCT dep.id, dep.name, dep.file_path, dep.intent, dep.pattern, dep.type
        LIMIT 50
        """
        return self.conn.execute(query, {"path": file_path}).get_as_df().to_dict("records")

    def close(self):
        self.conn.close()
        self.db.close()
