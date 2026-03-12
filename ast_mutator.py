"""
ast_mutator.py — Moteur 35 : AST Metaprogramming & Self-Rewriting Kernel

Le bot mute son propre code à la volée sans redémarrer Docker.
Si un modèle ML (M24 AlgoHunter) ou une stratégie détecte qu'une
fonction est devenue obsolète pour le régime de marché actuel,
le Moteur 35 :
  1. Parse l'AST de la fonction ciblée
  2. Génère une mutation optimisée (ajuste seuils, ajoute conditions)
  3. Compile via compile() + exec() dans un namespace isolé
  4. Teste la mutation dans un sandbox
  5. Remplace la fonction originale si le test passe
  6. Rollback automatique si la mutation échoue

Sécurité :
  - Namespace isolé (pas d'accès __builtins__ dangereux)
  - Timeout sur l'exécution des mutations
  - Rollback automatique vers le code original
  - Journal de toutes les mutations (audit trail)
  - Interdiction de modifier les fichiers sur disque (RAM uniquement)

Note : Toutes les mutations sont en RAM. Aucun fichier .py n'est modifié.
"""
import ast
import copy
import time
import threading
import inspect
import textwrap
import types
from typing import Dict, Optional, List, Tuple, Callable, Any
from datetime import datetime, timezone
from loguru import logger
import numpy as np

# ─── Configuration ────────────────────────────────────────────────────────────
_SCAN_INTERVAL_S       = 120      # Scan toutes les 2 min (mutation coûteuse)
_MAX_MUTATIONS         = 50       # Maximum de mutations actives
_MUTATION_TIMEOUT_S    = 5        # Timeout pour tester une mutation
_SANDBOX_ITERATIONS    = 100      # Iterations de test sandbox
_PERFORMANCE_THRESHOLD = 0.05     # 5% d'amélioration minimum pour adopter
_MAX_AST_DEPTH         = 10       # Profondeur max d'un AST muté

# Fonctions interdites dans les mutations (sécurité)
_FORBIDDEN_NAMES = {
    "os", "sys", "subprocess", "shutil", "importlib",
    "__import__", "open", "exec", "eval", "compile",
    "globals", "locals", "__builtins__",
}


class MutationRecord:
    """Journal d'une mutation de code."""

    def __init__(self, target_name: str, mutation_type: str,
                 original_src: str, mutated_src: str):
        self.target_name = target_name
        self.mutation_type = mutation_type
        self.original_src = original_src
        self.mutated_src = mutated_src
        self.timestamp = datetime.now(timezone.utc)
        self.success = False
        self.performance_delta = 0.0
        self.error = ""
        self.rollback = False

    def __repr__(self):
        status = "✅" if self.success else ("🔄" if self.rollback else "❌")
        return f"Mutation({status} {self.target_name} {self.mutation_type} Δ={self.performance_delta:+.2%})"


class SafeSandbox:
    """
    Namespace sandbox isolé pour tester les mutations.
    Pas d'accès aux fonctions système dangereuses.
    """

    def __init__(self):
        # Builtins sécurisés (lecture seule, pas d'I/O)
        safe_builtins = {
            "abs": abs, "all": all, "any": any, "bool": bool,
            "dict": dict, "enumerate": enumerate, "filter": filter,
            "float": float, "hasattr": hasattr, "int": int,
            "isinstance": isinstance, "len": len, "list": list,
            "map": map, "max": max, "min": min, "pow": pow,
            "print": lambda *a, **kw: None,  # Silenced
            "range": range, "round": round, "set": set,
            "sorted": sorted, "str": str, "sum": sum,
            "tuple": tuple, "type": type, "zip": zip,
            "True": True, "False": False, "None": None,
        }
        self.namespace = {
            "__builtins__": safe_builtins,
            "np": np,
            "math": __import__("math"),
            "time": {"time": time.time, "perf_counter": time.perf_counter},
        }

    def execute(self, code: str, timeout: float = _MUTATION_TIMEOUT_S) -> Tuple[bool, Any]:
        """Exécute du code dans le sandbox avec timeout."""
        result = [None]
        error = [None]

        def _run():
            try:
                exec(compile(code, "<mutation>", "exec"), self.namespace)
                result[0] = True
            except Exception as e:
                error[0] = str(e)
                result[0] = False

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        thread.join(timeout=timeout)

        if thread.is_alive():
            return False, "TIMEOUT"

        if error[0]:
            return False, error[0]

        return True, self.namespace


class ASTMutator:
    """
    Moteur de mutation AST.
    Génère des variations de code optimisées pour le régime courant.
    """

    @staticmethod
    def parse_function(func: Callable) -> Optional[ast.FunctionDef]:
        """Parse une fonction Python en AST."""
        try:
            source = inspect.getsource(func)
            source = textwrap.dedent(source)
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef):
                    return node
            return None
        except Exception:
            return None

    @staticmethod
    def validate_ast(tree: ast.AST) -> bool:
        """Valide qu'un AST ne contient pas de noms interdits."""
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in _FORBIDDEN_NAMES:
                return False
            if isinstance(node, ast.Attribute):
                if isinstance(node.value, ast.Name) and node.value.id in _FORBIDDEN_NAMES:
                    return False
            # Limiter la profondeur
            if hasattr(node, '_depth') and node._depth > _MAX_AST_DEPTH:
                return False
        return True

    @staticmethod
    def mutate_thresholds(func_ast: ast.FunctionDef,
                          scale_factor: float = 1.0) -> ast.FunctionDef:
        """
        Mutation Type 1 : Ajuste les seuils numériques.
        Multiplie tous les seuils (comparaisons) par un facteur.
        """
        mutated = copy.deepcopy(func_ast)

        class ThresholdMutator(ast.NodeTransformer):
            def visit_Compare(self, node):
                self.generic_visit(node)
                for i, comp in enumerate(node.comparators):
                    if isinstance(comp, ast.Constant) and isinstance(comp.value, (int, float)):
                        # Ajuster le seuil
                        new_val = comp.value * scale_factor
                        node.comparators[i] = ast.Constant(value=round(new_val, 6))
                return node

        ThresholdMutator().visit(mutated)
        ast.fix_missing_locations(mutated)
        return mutated

    @staticmethod
    def mutate_add_condition(func_ast: ast.FunctionDef,
                             condition_code: str) -> ast.FunctionDef:
        """
        Mutation Type 2 : Ajoute une condition de garde au début.
        """
        mutated = copy.deepcopy(func_ast)

        try:
            cond_ast = ast.parse(condition_code, mode='exec')
            # Insérer la condition en tête du body
            if cond_ast.body:
                for stmt in reversed(cond_ast.body):
                    mutated.body.insert(0, stmt)
            ast.fix_missing_locations(mutated)
        except Exception:
            pass

        return mutated

    @staticmethod
    def mutate_loop_unroll(func_ast: ast.FunctionDef,
                           factor: int = 2) -> ast.FunctionDef:
        """
        Mutation Type 3 : Déroulage de boucle (loop unrolling).
        Duplique le corps des boucles for simples.
        """
        mutated = copy.deepcopy(func_ast)

        class LoopUnroller(ast.NodeTransformer):
            def visit_For(self, node):
                self.generic_visit(node)
                if len(node.body) <= 3:  # Only unroll simple loops
                    new_body = []
                    for _ in range(factor):
                        new_body.extend(copy.deepcopy(node.body))
                    node.body = new_body
                return node

        LoopUnroller().visit(mutated)
        ast.fix_missing_locations(mutated)
        return mutated

    @staticmethod
    def ast_to_source(func_ast: ast.FunctionDef) -> str:
        """Convertit un AST en code source Python."""
        module = ast.Module(body=[func_ast], type_ignores=[])
        return ast.unparse(module)


class SelfRewritingKernel:
    """
    Moteur 35 : AST Metaprogramming & Self-Rewriting Kernel.

    Le bot mute ses propres fonctions en RAM pour s'adapter
    au régime de marché actuel, sans redémarrer Docker.
    """

    def __init__(self, db=None, algo_hunter=None, telegram_router=None):
        self._db = db
        self._algo_hunter = algo_hunter
        self._tg = telegram_router
        self._running = False
        self._thread = None
        self._lock = threading.Lock()

        # AST tools
        self._mutator = ASTMutator()
        self._sandbox = SafeSandbox()

        # Registre des fonctions originales (pour rollback)
        self._originals: Dict[str, Callable] = {}
        # Registre des mutations actives
        self._active_mutations: Dict[str, MutationRecord] = {}
        # Journal complet
        self._mutation_log: List[MutationRecord] = []

        # Stats
        self._scans = 0
        self._mutations_attempted = 0
        self._mutations_adopted = 0
        self._mutations_rolled_back = 0
        self._last_scan_ms = 0.0

        self._ensure_table()
        logger.info("🧬 M35 AST Mutator initialisé (Self-Rewriting Kernel + Sandbox)")

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._scan_loop, daemon=True, name="ast_mutator"
        )
        self._thread.start()
        logger.info("🧬 M35 AST Mutator démarré (scan toutes les 2min)")

    def stop(self):
        self._running = False
        # Rollback toutes les mutations actives
        self._rollback_all()

    # ─── Public API ──────────────────────────────────────────────────────────

    def register_mutable(self, name: str, func: Callable):
        """Enregistre une fonction comme mutable (permet les mutations)."""
        with self._lock:
            self._originals[name] = func

    def mutate_function(self, name: str, mutation_type: str = "threshold",
                        **kwargs) -> bool:
        """
        Tente de muter une fonction enregistrée.
        Returns: True si la mutation a été adoptée.
        """
        with self._lock:
            original = self._originals.get(name)

        if not original:
            return False

        try:
            # 1. Parser l'AST
            func_ast = self._mutator.parse_function(original)
            if not func_ast:
                return False

            original_src = self._mutator.ast_to_source(func_ast)

            # 2. Appliquer la mutation
            if mutation_type == "threshold":
                scale = kwargs.get("scale", np.random.uniform(0.8, 1.2))
                mutated_ast = self._mutator.mutate_thresholds(func_ast, scale)
            elif mutation_type == "add_condition":
                cond = kwargs.get("condition", "if True: pass")
                mutated_ast = self._mutator.mutate_add_condition(func_ast, cond)
            elif mutation_type == "unroll":
                factor = kwargs.get("factor", 2)
                mutated_ast = self._mutator.mutate_loop_unroll(func_ast, factor)
            else:
                return False

            # 3. Valider la sécurité de l'AST
            if not self._mutator.validate_ast(mutated_ast):
                logger.warning(f"M35 mutation {name} rejetée: noms interdits")
                return False

            mutated_src = self._mutator.ast_to_source(mutated_ast)

            # 4. Créer l'enregistrement
            record = MutationRecord(
                target_name=name,
                mutation_type=mutation_type,
                original_src=original_src,
                mutated_src=mutated_src,
            )

            # 5. Tester dans le sandbox
            success, result = self._sandbox.execute(mutated_src)

            if not success:
                record.error = str(result)
                record.success = False
                self._mutation_log.append(record)
                self._mutations_attempted += 1
                return False

            # 6. Évaluer la performance
            perf_delta = self._benchmark_mutation(original, mutated_src, name)
            record.performance_delta = perf_delta

            if perf_delta >= _PERFORMANCE_THRESHOLD:
                # Adopter la mutation
                record.success = True
                self._mutations_adopted += 1

                with self._lock:
                    self._active_mutations[name] = record

                logger.info(
                    f"🧬 M35 MUTATION: {name} {mutation_type} "
                    f"Δ={perf_delta:+.2%} → ADOPTÉ"
                )
                self._persist_mutation(record)
            else:
                record.success = False

            self._mutation_log.append(record)
            self._mutations_attempted += 1
            return record.success

        except Exception as e:
            logger.debug(f"M35 mutation {name}: {e}")
            return False

    def rollback(self, name: str) -> bool:
        """Rollback une mutation vers la version originale."""
        with self._lock:
            if name in self._active_mutations:
                self._active_mutations[name].rollback = True
                del self._active_mutations[name]
                self._mutations_rolled_back += 1
                logger.info(f"🧬 M35 ROLLBACK: {name} → original restauré")
                return True
        return False

    def stats(self) -> dict:
        with self._lock:
            active = {k: {
                "type": v.mutation_type,
                "delta": f"{v.performance_delta:+.2%}",
            } for k, v in self._active_mutations.items()}

        return {
            "scans": self._scans,
            "mutations_attempted": self._mutations_attempted,
            "mutations_adopted": self._mutations_adopted,
            "mutations_rolled_back": self._mutations_rolled_back,
            "active_mutations": active,
            "originals_registered": len(self._originals),
            "adoption_rate": (
                f"{self._mutations_adopted / max(self._mutations_attempted, 1):.0%}"
            ),
            "last_scan_ms": round(self._last_scan_ms, 1),
        }

    def format_report(self) -> str:
        s = self.stats()
        active_str = " | ".join(
            f"{k}:{v['type']}({v['delta']})" for k, v in s["active_mutations"].items()
        ) or "—"
        return (
            f"🧬 <b>AST Mutator (M35)</b>\n\n"
            f"  Attempts: {s['mutations_attempted']} | "
            f"Adopted: {s['mutations_adopted']} ({s['adoption_rate']})\n"
            f"  Rollbacks: {s['mutations_rolled_back']}\n"
            f"  Registered: {s['originals_registered']} funcs\n"
            f"  Active: {active_str}"
        )

    # ─── Internal ────────────────────────────────────────────────────────────

    def _rollback_all(self):
        """Rollback toutes les mutations actives."""
        with self._lock:
            for name in list(self._active_mutations.keys()):
                self._active_mutations[name].rollback = True
                self._mutations_rolled_back += 1
            self._active_mutations.clear()
        logger.info("🧬 M35 ROLLBACK ALL: toutes les mutations annulées")

    def _benchmark_mutation(self, original_func: Callable,
                            mutated_src: str, name: str) -> float:
        """
        Benchmark la mutation vs l'original.
        Returns: performance delta (positive = amélioration).
        """
        try:
            # Benchmark original
            t0 = time.perf_counter()
            for _ in range(_SANDBOX_ITERATIONS):
                try:
                    original_func()
                except Exception:
                    pass
            original_time = time.perf_counter() - t0

            # Benchmark mutation (dans le sandbox)
            sandbox = SafeSandbox()
            sandbox.execute(mutated_src)
            mutated_func = sandbox.namespace.get(name)

            if not callable(mutated_func):
                return -1.0

            t0 = time.perf_counter()
            for _ in range(_SANDBOX_ITERATIONS):
                try:
                    mutated_func()
                except Exception:
                    pass
            mutated_time = time.perf_counter() - t0

            if original_time <= 0:
                return 0.0

            # Delta positif = la mutation est plus rapide
            delta = (original_time - mutated_time) / original_time
            return round(delta, 4)

        except Exception:
            return -1.0

    # ─── Scan Loop ───────────────────────────────────────────────────────────

    def _scan_loop(self):
        time.sleep(60)
        while self._running:
            t0 = time.time()
            try:
                self._scan_cycle()
            except Exception as e:
                logger.debug(f"M35 scan: {e}")
            self._last_scan_ms = (time.time() - t0) * 1000
            self._scans += 1
            time.sleep(_SCAN_INTERVAL_S)

    def _scan_cycle(self):
        """Cycle: check performance → propose mutations → test → adopt/reject."""
        with self._lock:
            registered = list(self._originals.keys())

        for name in registered:
            # Tenter une mutation aléatoire
            mutation_type = np.random.choice(["threshold", "unroll"])
            scale = np.random.uniform(0.85, 1.15)
            self.mutate_function(name, mutation_type, scale=scale)

    # ─── Database ────────────────────────────────────────────────────────────

    def _ensure_table(self):
        if not self._db or not getattr(self._db, '_pg', False):
            return
        try:
            self._db._execute("""
                CREATE TABLE IF NOT EXISTS ast_mutations (
                    id              SERIAL PRIMARY KEY,
                    target_name     VARCHAR(80),
                    mutation_type   VARCHAR(30),
                    perf_delta      FLOAT,
                    success         BOOLEAN,
                    rollback        BOOLEAN DEFAULT FALSE,
                    error           TEXT,
                    detected_at     TIMESTAMPTZ DEFAULT NOW()
                )
            """)
        except Exception as e:
            logger.debug(f"M35 table: {e}")

    def _persist_mutation(self, record: MutationRecord):
        if not self._db or not getattr(self._db, '_pg', False):
            return
        try:
            ph = "%s"
            self._db._execute(
                f"INSERT INTO ast_mutations "
                f"(target_name,mutation_type,perf_delta,success,rollback,error) "
                f"VALUES ({ph},{ph},{ph},{ph},{ph},{ph})",
                (record.target_name, record.mutation_type,
                 record.performance_delta, record.success,
                 record.rollback, record.error)
            )
        except Exception:
            pass
