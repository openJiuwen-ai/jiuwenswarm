# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

from concurrent.futures import ThreadPoolExecutor, as_completed
import time


class TreeNode:
    def __init__(self, data, score, results, history=None):
        self.data = data
        self.score = score
        self.results = results
        self.history = history if history is not None else []
        self.history = self.history + [results]
        self.parent = None
        self.children = []

    def __repr__(self):
        depth = self.get_depth()
        string = f'{"    "*depth}it={depth} score={self.score:.1f} data="{self.data}"'
        for child in self.children:
            string += '\n' + repr(child)
        return string

    def get_depth(self):
        if self.parent is None:
            return 0
        return self.parent.get_depth() + 1



class BeamSearch:
    def __init__(
            self, 
            method, 
            beam_width, 
            expand_num, 
            max_depth, 
            num_workers=1, 
            verbose=False, 
            early_stop=True, 
            check_valid=False, 
            max_score=100., 
            top_k=1
        ):
        self.method = method
        self.beam_width = beam_width
        self.max_depth = max_depth
        self.expand_num = expand_num
        self.num_workers = num_workers
        if self.num_workers > 1:
            self.pool = ThreadPoolExecutor(self.num_workers)
        self.verbose = verbose
        self.early_stop = early_stop
        self.check_valid = check_valid
        self.max_score = max_score
        self.num_retry = 1
        self.top_k = top_k
        self.timeout = 600

    def search(self, tool):
        start_time = time.time()
        examples = self.method.get_examples(tool) if callable(getattr(self.method, 'get_examples', None)) else None

        # initial root node generation / evaluation
        root = None
        for _ in range(self.num_retry):
            results, data, score = self.method.step(
                tool=tool,
                examples=examples,
                it=0,
            )

            if self.check_valid and score == -1:
                continue

            root = TreeNode(
                data=data,
                score=score,
                results=results,
            )
            break

        if root is None:
            raise RuntimeError("Failed to generate a valid root node after retries.")

        beam_list = [root]
        best_nodes = [root]

        # expand and prune
        for depth in range(1, self.max_depth + 1):
            if time.time() - start_time > self.timeout:
                nodes_sorted = sorted(best_nodes, reverse=True, key=lambda x: x.score)[:self.top_k]
                return [node.history for node in nodes_sorted]
            if self.early_stop and self.check_early_stop(beam_list, max_score=self.max_score, k=self.top_k):
                break
            beam_list = self.expand(beam_list, tool, examples, depth)
            beam_list = self.prune(beam_list)
            best_nodes += beam_list

        nodes_sorted = sorted([node for node in best_nodes if node.get_depth() > 0], 
                              reverse=True, key=lambda x: x.score)[:self.top_k]

        return [node.history for node in nodes_sorted]

    def expand(self, beam_list, tool, examples, depth):
        def expand_single_step(node, tool, examples, depth):
            new_node = None
            for _ in range(self.num_retry):
                output, data, score = self.method.step(
                    tool=tool,
                    examples=examples,
                    prev_outputs=node.history,
                    it=depth,
                )
                if self.check_valid and score == -1:
                    continue

                new_node = TreeNode(
                    data=data,
                    score=score,
                    results=output,
                    history=node.history,
                )
                new_node.parent = node
                node.children.append(new_node)
                break

            if new_node is None:
                raise RuntimeError
            return new_node

        new_beam_list = []
        futures = []
        for node in beam_list:
            for _ in range(self.expand_num):
                if self.num_workers == 1:
                    new_node = expand_single_step(node, tool, examples, depth)
                    new_beam_list.append(new_node)
                else:
                    futures.append(
                        self.pool.submit(
                            expand_single_step, node, tool, examples, depth
                        )
                    )
        if self.num_workers > 1:
            for thd in as_completed(futures):
                e = thd.exception()
                if e is None:
                    new_node = thd.result()
                    new_beam_list.append(new_node)
                else:
                    continue

        return new_beam_list

    def prune(self, beam_list):
        sorted_beam_list = sorted(beam_list, reverse=True, key=lambda x: x.score)
        return sorted_beam_list[:self.beam_width]

    def check_early_stop(self, beam_list, max_score=100., k=1):
        x = self.top_k
        if len(beam_list) < k:
            return False
        for node in beam_list[:k]:
            if node.score < max_score:
                return False
        return True
