"""熔窑侧的命名空间与链路拓扑。"""

from .registry import Namespace, NamespaceRegistry
from .topology import Topology, build_default_topology

__all__ = ["Namespace", "NamespaceRegistry", "Topology", "build_default_topology"]
