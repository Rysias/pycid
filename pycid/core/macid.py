from __future__ import annotations

import copy
import itertools
from typing import Any, Dict, Iterable, List, Optional, Tuple
from warnings import warn

import networkx as nx
import pygambit
from pgmpy.factors.discrete import TabularCPD

from pycid.core.cpd import DecisionDomain, StochasticFunctionCPD
from pycid.core.macid_base import MACIDBase
from pycid.core.relevance_graph import CondensedRelevanceGraph
from pycid.export.gambit import behavior_to_cpd, macid_to_efg

Outcome = Any


class MACID(MACIDBase):
    """A Multi-Agent Causal Influence Diagram"""

    def get_ne(self, solver: Optional[str] = "enumpure") -> List[List[StochasticFunctionCPD]]:
        """
        Return a list of Nash equilbiria in the MACID. By default, this finds all pure NE using the 'enumpure'
        pygambit solver. Use the 'solver' argument to change this behavior.
        Recommended Usage:
        - 2-player games: solver='enummixed' to find all mixed NE
        - N-player games: solver='enumpure' if one wants to find all pure NE, or solver={'simpdiv', 'ipa', 'gnm'}
        if one wants to find at least one mixed NE. See pygambit docs for details
        https://gambitproject.readthedocs.io/en/latest/pyapi.html#module-pygambit.nash
        - solver can be any of the pygambit solvers (default: "enumpure" - finds all pure NEs).
            - "enumpure": enumerate all pure NEs in the MACID.
                - for arbitrary N-player games
            - "enummixed": Valid for enumerate all mixed NEs in the MACID by computing the
              extreme points of the set of equilibria.
                - for 2-player games only
            - "lcp": Compute NE using the Linear Complementarity Program (LCP) solver.
                - for 2-player games only
            - "lp": Compute (one) NE using the Linear Programming solver.
                - for 2-player, constant sum games only
            - "simpdiv": Compute one mixed NE using the Simplicial Subdivision.
                - for arbitrary N-player games
            - "ipa": Compute one mixed NE using the Iterative Partial Assignment solver
                - for arbitrary N-player games
            - "gnm": Compute one mixed NE using the global newton method
                - for arbitrary N-player games

        Each NE comes as a list of FunctionCPDs, one for each decision node in the MACID.
        """
        return self.get_ne_in_sg(solver=solver)

    def get_ne_in_sg(
        self,
        decisions_in_sg: Optional[Iterable[str]] = None,
        solver: Optional[str] = "enumpure",
    ) -> List[List[StochasticFunctionCPD]]:
        """
        Return a list of NE in a MACID subgame. By default, this finds all pure NE in an arbitray N-player game.
        Use the 'solver' argument to change this behavior (see get_ne method for details).
        - Each NE comes as a list of FunctionCPDs, one for each decision node in the MAID subgame.
        - If decisions_in_sg is not specified, this method finds NE in the full MACID.
        - If the MACID being operated on already has function CPDs for some decision nodes, it is
        assumed that these have already been optimised and so these are not changed.
        """
        # TODO: Check that the decisions in decisions_in_sg actually make up a MAID subgame
        if decisions_in_sg is None:
            decisions_in_sg = self.decisions
        else:
            decisions_in_sg = set(decisions_in_sg)  # For efficient membership checks
        agents_in_sg = list({self.decision_agent[dec] for dec in decisions_in_sg})

        # impute random decisions to non-instantiated, irrelevant decision nodes
        sg_macid = self.copy()
        for d in sg_macid.decisions:
            if not sg_macid.is_s_reachable(decisions_in_sg, d) and isinstance(sg_macid.get_cpds(d), DecisionDomain):
                sg_macid.impute_random_decision(d)

        # pygambit NE solver
        efg, parents_to_infoset = macid_to_efg(sg_macid, decisions_in_sg, agents_in_sg)
        ne_behavior_strategies = self._pygambit_ne_solver(efg, solver_override=solver)
        ne_in_sg = [
            behavior_to_cpd(sg_macid, parents_to_infoset, strat, decisions_in_sg) for strat in ne_behavior_strategies
        ]

        return ne_in_sg

    def get_spe(self, solver: Optional[str] = None) -> List[List[StochasticFunctionCPD]]:
        """Return a list of subgame perfect Nash equilbiria (SPE) in the MACIM.
        By default, this finds mixed SPE using the 'enummixed' pygambit solver for 2-player games, and
        pure SPE using the 'enumpure' pygambit solver for N-player games. If pure NE do not exist,
        it uses the 'simpdiv' solver to find a mixed NE.
        Use the 'solver' argument to change this behavior (see get_ne method for details).
        - Each SPE comes as a list of FunctionCPDs, one for each decision node in the MACID.
        """
        spes: List[List[StochasticFunctionCPD]] = [[]]

        macid = self.copy()
        # backwards induction over the sccs in the condensed relevance graph (handling tie-breaks)
        for scc in reversed(CondensedRelevanceGraph(macid).get_scc_topological_ordering()):
            extended_spes = []
            for partial_profile in spes:
                macid.add_cpds(*partial_profile)
                ne_in_sg = macid.get_ne_in_sg(decisions_in_sg=scc, solver=solver)
                for ne in ne_in_sg:
                    extended_spes.append(partial_profile + list(ne))
            spes = extended_spes
        return spes

    def decs_in_each_maid_subgame(self) -> List[set]:
        """
        Return a list giving the set of decision nodes in each MAID subgame of the original MAID.
        """
        con_rel = CondensedRelevanceGraph(self)
        con_rel_sccs = con_rel.nodes  # the nodes of the condensed relevance graph are the maximal sccs of the MA(C)ID
        powerset = list(
            itertools.chain.from_iterable(
                itertools.combinations(con_rel_sccs, r) for r in range(1, len(con_rel_sccs) + 1)
            )
        )
        con_rel_subgames = copy.deepcopy(powerset)
        for subset in powerset:
            for node in subset:
                if not nx.descendants(con_rel, node).issubset(subset) and subset in con_rel_subgames:
                    con_rel_subgames.remove(subset)

        dec_subgames = [
            [con_rel.get_decisions_in_scc()[scc] for scc in con_rel_subgame] for con_rel_subgame in con_rel_subgames
        ]

        return [set(itertools.chain.from_iterable(i)) for i in dec_subgames]

    def joint_pure_policies(self, decisions: Iterable[str]) -> List[Tuple[StochasticFunctionCPD, ...]]:
        """return a list of tuples of all joint pure policies in the MACID. A joint pure policy assigns a
        pure decision rule to every decision node in the MACID."""
        all_dec_decision_rules = list(map(self.pure_decision_rules, decisions))
        return list(itertools.product(*all_dec_decision_rules))

    def policy_profile_assignment(self, partial_policy: Iterable[StochasticFunctionCPD]) -> Dict:
        """Return a dictionary with the joint or partial policy profile assigned -
        ie a decision rule for each of the MACIM's decision nodes."""
        pp: Dict[str, Optional[TabularCPD]] = {d: None for d in self.decisions}
        pp.update({cpd.variable: cpd for cpd in partial_policy})
        return pp

    def copy_without_cpds(self) -> MACID:
        """copy the MACID structure"""
        new = MACID()
        new.add_nodes_from(self.nodes)
        new.add_edges_from(self.edges)
        for agent in self.agents:
            for decision in self.agent_decisions[agent]:
                new.make_decision(decision, agent)
            for utility in self.agent_utilities[agent]:
                new.make_utility(utility, agent)
        return new

    def _pygambit_ne_solver(
        self, game: pygambit.Game, solver_override: Optional[str] = None
    ) -> List[pygambit.lib.libgambit.MixedStrategyProfile]:
        """Uses pygambit to find the Nash equilibria of the EFG.
        Default solver is enummixed for 2 player games. This finds all NEs.
        For non-2-player games, the default is enumpure which finds all pure NEs.
        If no pure NEs are found, then simpdiv is used to find a mixed NE if it exists.
        If a specific solver is desired, it can be passed as a string, but if it is not compatible
        with the game, a warning will be raised and it will be ignored. We need to do this because
        enummixed is not compatible for non-2-player games.
        """
        # check if a 2 player game, if so, default to enummixed, else enumpure
        two_player = True if len(game.players) == 2 else False
        if solver_override is None:
            solver = "enummixed" if two_player else "enumpure"
        elif solver_override in ["enummixed", "lcp", "lp"] and not two_player:
            warn(f"Solver {solver_override} not allowed for non-2 player games. Using 'enumpure' instead.")
            solver = "enumpure"
        else:
            solver = solver_override

        if solver == "enummixed":
            mixed_strategies = pygambit.nash.enummixed_solve(game, rational=False)
        elif solver == "enumpure":
            mixed_strategies = pygambit.nash.enumpure_solve(game)
            # if no pure NEs found, try simpdiv if not overridden by user
            if len(mixed_strategies) == 0 and solver_override is None:
                warn("No pure NEs found using enumpure. Trying simpdiv.")
                mixed_strategies = pygambit.nash.simpdiv_solve(game)
        elif solver == "lcp":
            mixed_strategies = pygambit.nash.lcp_solve(game, rational=False)
        elif solver == "lp":
            mixed_strategies = pygambit.nash.lp_solve(game, rational=False)
        elif solver == "simpdiv":
            mixed_strategies = pygambit.nash.simpdiv_solve(game)
        elif solver == "ipa":
            mixed_strategies = pygambit.nash.ipa_solve(game)
        elif solver == "gnm":
            mixed_strategies = pygambit.nash.gnm_solve(game)
        else:
            raise ValueError(f"Solver {solver} not recognised")
        # convert to behavior strategies
        behavior_strategies = [x.as_behavior() if solver not in ["lp", "lcp"] else x for x in mixed_strategies]

        return behavior_strategies
