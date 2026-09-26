"""熔窑工艺链路拓扑。

控制台需要按真实工艺顺序展示链路，投料、点火、成型与退火的顺序检查也
需要知道"谁在谁的上游"。拓扑只描述路径依赖，不持有设备对象，避免与组件
包互相引用。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Topology:
    """有向链路：``links`` 表示 (upstream, downstream) 的顺序约束。"""

    nodes: list[str] = field(default_factory=list)
    links: list[tuple[str, str]] = field(default_factory=list)

    def add_node(self, name: str) -> None:
        if name not in self.nodes:
            self.nodes.append(name)

    def add_link(self, upstream: str, downstream: str) -> None:
        """登记一条顺序约束：upstream 必须先于 downstream 动作。"""

        self.add_node(upstream)
        self.add_node(downstream)
        self.links.append((upstream, downstream))

    def upstream_of(self, name: str) -> list[str]:
        return [up for up, down in self.links if down == name]

    def downstream_of(self, name: str) -> list[str]:
        return [down for up, down in self.links if up == name]

    def order_index(self, name: str) -> int:
        """返回节点在链路中的首次出现位置，用于控制台排序。"""

        return self.nodes.index(name)

    def requires_before(self, first: str, second: str) -> bool:
        """判断 ``first`` 是否在 ``second`` 的上游（传递闭包）。"""

        seen: set[str] = set()
        pending = [second]
        while pending:
            current = pending.pop()
            for upstream in self.upstream_of(current):
                if upstream == first:
                    return True
                if upstream not in seen:
                    seen.add(upstream)
                    pending.append(upstream)
        return False

    def as_dict(self) -> dict:
        return {
            "nodes": [
                {"name": name, "index": self.order_index(name)} for name in self.nodes
            ],
            "links": [{"upstream": up, "downstream": down} for up, down in self.links],
        }


def build_default_topology() -> Topology:
    """按工艺主链与控制顺序建立默认拓扑。"""

    topology = Topology()
    chain = [
        ("batch", "furnace"),
        ("burner", "furnace"),
        ("furnace", "forehearth"),
        ("furnace", "temp"),
        ("temp", "forehearth"),
        ("temp", "lehr"),
        ("forehearth", "feeder"),
        ("feeder", "mold"),
        ("mold", "lehr"),
        ("burner", "console"),
        ("furnace", "console"),
        ("lehr", "console"),
    ]
    for upstream, downstream in chain:
        topology.add_link(upstream, downstream)
    return topology
