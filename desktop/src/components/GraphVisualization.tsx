import { useCallback, useEffect, useRef, useState } from "react";
import ForceGraph3D from "react-force-graph-3d";
import type { GraphData } from "react-force-graph-3d";
import { fetchGraphScaffold, fetchGraphContext, type GraphScaffoldResponse } from "../lib/api";

type GraphNode = {
  id: string;
  node_kind: string;
  title: string;
  summary?: string;
  x?: number;
  y?: number;
  z?: number;
  vx?: number;
  vy?: number;
  vz?: number;
  fx?: number;
  fy?: number;
  fz?: number;
};

type GraphLink = {
  source: string | GraphNode;
  target: string | GraphNode;
  rel_kind: string;
};

type NodeEvidence = {
  source_id: string;
  path: string;
  snippet: string;
  relevance: number;
};

type NodeRelationship = {
  related_node_id: string;
  related_node_title: string;
  related_node_kind: string;
  rel_kind: string;
};

export function GraphVisualization() {
  const [graphData, setGraphData] = useState<GraphData | null>(null);
  const [loading, setLoading] = useState(true);
  const [selectedNode, setSelectedNode] = useState<GraphNode | null>(null);
  const [nodeEvidence, setNodeEvidence] = useState<NodeEvidence[]>([]);
  const [nodeRelationships, setNodeRelationships] = useState<NodeRelationship[]>([]);
  const [loadingDetails, setLoadingDetails] = useState(false);
  const [nodeFilter, setNodeFilter] = useState<string>("all");
  const [showLabels, setShowLabels] = useState(true);
  const [dimensions, setDimensions] = useState({ width: 0, height: 0 });
  const containerRef = useRef<HTMLDivElement>(null);
  const graphRef = useRef<any>(null);
  const fitTimersRef = useRef<ReturnType<typeof setTimeout>[]>([]);

  const clearFitTimers = useCallback(() => {
    fitTimersRef.current.forEach(clearTimeout);
    fitTimersRef.current = [];
  }, []);

  const fitGraphToView = useCallback(() => {
    graphRef.current?.zoomToFit(400, 64);
  }, []);

  const scheduleFitGraphToView = useCallback(() => {
    clearFitTimers();
    fitGraphToView();
    requestAnimationFrame(fitGraphToView);
    fitTimersRef.current = [150, 450, 900].map((ms) =>
      setTimeout(fitGraphToView, ms),
    );
  }, [clearFitTimers, fitGraphToView]);

  useEffect(() => () => clearFitTimers(), [clearFitTimers]);

  useEffect(() => {
    loadGraphData();
  }, []);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const updateDimensions = () => {
      const { width, height } = container.getBoundingClientRect();
      setDimensions({
        width: Math.floor(width),
        height: Math.floor(height),
      });
    };

    updateDimensions();
    const observer = new ResizeObserver(updateDimensions);
    observer.observe(container);
    return () => observer.disconnect();
  }, [loading]);

  useEffect(() => {
    if (loading || !graphData?.nodes.length || dimensions.width <= 0 || dimensions.height <= 0) {
      return;
    }
    scheduleFitGraphToView();
    return clearFitTimers;
  }, [loading, graphData, dimensions, nodeFilter, scheduleFitGraphToView, clearFitTimers]);

  const loadGraphData = async () => {
    setLoading(true);
    try {
      const scaffold: GraphScaffoldResponse = await fetchGraphScaffold();
      
      // Convert scaffold tree to force-graph format
      const nodes: GraphNode[] = [];
      const links: GraphLink[] = [];
      const nodeSet = new Set<string>();

      function processNode(node: any, parentId?: string) {
        if (!node || nodeSet.has(node.node_id)) return;
        nodeSet.add(node.node_id);

        nodes.push({
          id: node.node_id,
          node_kind: node.node_kind,
          title: node.title,
          summary: node.summary,
        });

        if (parentId) {
          links.push({
            source: parentId,
            target: node.node_id,
            rel_kind: "parent-child",
          });
        }

        if (node.children) {
          node.children.forEach((child: any) => processNode(child, node.node_id));
        }
      }

      if (scaffold.root) {
        processNode(scaffold.root);
      }

      scaffold.tree.forEach((node: any) => processNode(node));

      setGraphData({ nodes, links });
    } catch (error) {
      console.error("Failed to load graph data:", error);
    } finally {
      setLoading(false);
    }
  };

  const loadNodeDetails = async (node: GraphNode) => {
    setLoadingDetails(true);
    try {
      const context = await fetchGraphContext(node.title);
      
      // Extract evidence from context
      const evidence: NodeEvidence[] = [];
      if (context.recent_ambient) {
        context.recent_ambient.forEach((item: any) => {
          if (item.snippet) {
            evidence.push({
              source_id: item.source_id || "unknown",
              path: item.path || "unknown",
              snippet: item.snippet,
              relevance: item.relevance || 0.5,
            });
          }
        });
      }
      setNodeEvidence(evidence.slice(0, 5)); // Limit to top 5

      // Extract relationships from graph data
      const relationships: NodeRelationship[] = [];
      if (graphData) {
        graphData.links.forEach((link: any) => {
          const sourceId = (link.source as any).id || link.source;
          const targetId = (link.target as any).id || link.target;
          if (sourceId === node.id || targetId === node.id) {
            const relatedId = sourceId === node.id ? targetId : sourceId;
            const relatedNode = graphData.nodes.find((n: any) => n.id === relatedId);
            if (relatedNode) {
              relationships.push({
                related_node_id: relatedNode.id as string,
                related_node_title: relatedNode.title,
                related_node_kind: relatedNode.node_kind,
                rel_kind: link.rel_kind,
              });
            }
          }
        });
      }
      setNodeRelationships(relationships);
    } catch (error) {
      console.error("Failed to load node details:", error);
    } finally {
      setLoadingDetails(false);
    }
  };

  const handleNodeClick = (node: GraphNode) => {
    setSelectedNode(node);
    void loadNodeDetails(node);
  };

  const getNodeColor = (node: GraphNode): string => {
    const kind = node.node_kind?.toLowerCase() || "";
    if (kind.includes("person") || kind.includes("user")) return "#3b82f6"; // blue
    if (kind.includes("project") || kind.includes("job")) return "#10b981"; // green
    if (kind.includes("obligation") || kind.includes("task")) return "#f59e0b"; // amber
    if (kind.includes("topic") || kind.includes("concept")) return "#8b5cf6"; // purple
    return "#6b7280"; // gray
  };

  const filteredNodes = graphData?.nodes.filter((node: any) => {
    if (nodeFilter === "all") return true;
    return (node as GraphNode).node_kind?.toLowerCase().includes(nodeFilter);
  }) || [];

  const filteredNodeIds = new Set(filteredNodes.map(n => n.id));
  const filteredLinks = graphData?.links.filter((link: any) => 
    filteredNodeIds.has((link.source as any).id || link.source) && filteredNodeIds.has((link.target as any).id || link.target)
  ) || [];

  return (
    <div className="flex h-[500px] w-full gap-4">
      {/* Graph canvas */}
      <div
        ref={containerRef}
        className="relative flex-1 min-w-0 h-full rounded-2xl border border-border bg-card overflow-hidden"
      >
        {loading ? (
          <div className="flex items-center justify-center h-full">
            <div className="text-muted-foreground">Loading graph...</div>
          </div>
        ) : !graphData || graphData.nodes.length === 0 ? (
          <div className="flex items-center justify-center h-full">
            <div className="text-muted-foreground text-center">
              <p className="mb-2">No graph data available</p>
              <p className="text-sm">Build your knowledge graph first to visualize nodes and edges</p>
            </div>
          </div>
        ) : dimensions.width > 0 && dimensions.height > 0 ? (
          <ForceGraph3D
            ref={graphRef}
            width={dimensions.width}
            height={dimensions.height}
            graphData={{ nodes: filteredNodes as any, links: filteredLinks as any }}
            nodeColor={(node: any) => getNodeColor(node as GraphNode)}
            nodeLabel={(node: any) => showLabels ? (node as GraphNode).title : ""}
            nodeRelSize={3}
            linkWidth={1.5}
            linkColor="#94a3b8"
            onNodeClick={handleNodeClick}
            onNodeDragEnd={(node: any) => {
              node.fx = node.x;
              node.fy = node.y;
              node.fz = node.z;
            }}
            onEngineStop={scheduleFitGraphToView}
            enableNodeDrag={true}
            backgroundColor="#0a0a0a"
            showNavInfo={false}
            warmupTicks={100}
            cooldownTicks={0}
            d3AlphaDecay={0.02}
            d3VelocityDecay={0.3}
          />
        ) : null}
      </div>

      {/* Controls sidebar */}
      <aside className="w-72 shrink-0 overflow-y-auto rounded-2xl border border-border bg-card p-4 text-foreground">
        <h3 className="mb-4 text-lg font-medium">Controls</h3>
        
        {/* Filter */}
        <div className="mb-4">
          <label className="mb-2 block text-xs text-muted-foreground">Filter by type</label>
          <select
            value={nodeFilter}
            onChange={(e) => setNodeFilter(e.target.value)}
            className="w-full rounded-lg border border-border bg-background px-2 py-1.5 text-sm hover:bg-accent"
          >
            <option value="all">All Types</option>
            <option value="person">People</option>
            <option value="project">Projects</option>
            <option value="obligation">Obligations</option>
            <option value="topic">Topics</option>
          </select>
        </div>

        {/* Toggle labels */}
        <div className="mb-4 flex items-center justify-between">
          <label className="text-sm font-medium">Show labels</label>
          <button
            onClick={() => setShowLabels(!showLabels)}
            className={`shrink-0 rounded px-3 py-1.5 text-xs font-medium transition-colors ${
              showLabels
                ? "bg-primary text-primary-foreground hover:bg-primary/90"
                : "bg-muted text-muted-foreground hover:bg-muted/80"
            }`}
          >
            {showLabels ? "On" : "Off"}
          </button>
        </div>

        <div className="mb-4">
          <button
            onClick={fitGraphToView}
            className="w-full rounded-lg border border-border px-3 py-2 text-sm hover:bg-accent"
          >
            Recenter graph
          </button>
        </div>

        {/* Stats */}
        <div className="mb-4 rounded-lg bg-muted/50 p-3">
          <div className="flex justify-between text-sm">
            <span className="text-muted-foreground">Nodes</span>
            <span className="font-medium">{filteredNodes.length}</span>
          </div>
          <div className="flex justify-between text-sm">
            <span className="text-muted-foreground">Edges</span>
            <span className="font-medium">{filteredLinks.length}</span>
          </div>
        </div>

        {/* Selected node details */}
        {selectedNode && (
          <div className="mb-4 rounded-lg bg-muted/30 p-3">
            <h4 className="mb-2 font-medium">Selected node</h4>
            <div className="space-y-1 text-sm">
              <div>
                <span className="text-muted-foreground">Type:</span> {selectedNode.node_kind}
              </div>
              <div>
                <span className="text-muted-foreground">Title:</span> {selectedNode.title}
              </div>
              {selectedNode.summary && (
                <div>
                  <span className="text-muted-foreground">Summary:</span>
                  <p className="mt-1 text-foreground/90">{selectedNode.summary}</p>
                </div>
              )}
            </div>

            {loadingDetails ? (
              <div className="mt-2 text-sm text-muted-foreground">Loading details...</div>
            ) : (
              <>
                {nodeRelationships.length > 0 && (
                  <div className="mt-3">
                    <h5 className="mb-1 text-sm font-medium">Relationships ({nodeRelationships.length})</h5>
                    <div className="max-h-24 space-y-1 overflow-y-auto">
                      {nodeRelationships.map((rel, idx) => (
                        <div key={idx} className="cursor-pointer rounded-lg bg-muted/50 p-2 text-xs hover:bg-accent/40">
                          <div className="font-medium">{rel.related_node_title}</div>
                          <div className="text-muted-foreground">{rel.related_node_kind} · {rel.rel_kind}</div>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {nodeEvidence.length > 0 && (
                  <div className="mt-3">
                    <h5 className="mb-1 text-sm font-medium">Evidence ({nodeEvidence.length})</h5>
                    <div className="max-h-24 space-y-1 overflow-y-auto">
                      {nodeEvidence.map((ev, idx) => (
                        <div key={idx} className="rounded-lg bg-muted/50 p-2 text-xs">
                          <div className="truncate font-medium">{ev.path}</div>
                          <div className="truncate text-muted-foreground">{ev.snippet}</div>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </>
            )}

            <button
              onClick={() => {
                setSelectedNode(null);
                setNodeEvidence([]);
                setNodeRelationships([]);
              }}
              className="mt-3 w-full rounded-lg border border-border px-3 py-1.5 text-sm hover:bg-accent"
            >
              Clear selection
            </button>
          </div>
        )}

        {/* Legend */}
        <div>
          <h4 className="mb-2 font-medium">Legend</h4>
          <div className="flex flex-col gap-2">
            <div className="flex items-center gap-2">
              <div className="h-3 w-3 rounded-full bg-blue-500" />
              <span className="text-sm">People</span>
            </div>
            <div className="flex items-center gap-2">
              <div className="h-3 w-3 rounded-full bg-green-500" />
              <span className="text-sm">Projects</span>
            </div>
            <div className="flex items-center gap-2">
              <div className="h-3 w-3 rounded-full bg-amber-500" />
              <span className="text-sm">Obligations</span>
            </div>
            <div className="flex items-center gap-2">
              <div className="h-3 w-3 rounded-full bg-purple-500" />
              <span className="text-sm">Topics</span>
            </div>
            <div className="flex items-center gap-2">
              <div className="h-3 w-3 rounded-full bg-slate-400" />
              <span className="text-sm">Other</span>
            </div>
          </div>
        </div>
      </aside>
    </div>
  );
}
