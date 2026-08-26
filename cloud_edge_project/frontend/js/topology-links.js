/* Fixed Sender-facing topology rows and latest-batch usage mapping. */
(function (root) {
  "use strict";

  const SENDERS = ["sender_01", "sender_02", "sender_03"];
  const TARGETS = [
    { id: "scheduler", protocol: "http" },
    { id: "edge_01", protocol: "mqtt" },
    { id: "edge_02", protocol: "mqtt" },
  ];

  function linkId(senderId, target) {
    return senderId + "__to__" + target.id + "__" + target.protocol;
  }

  function buildSenderLinkRows(networkLinks, batch) {
    const links = Array.isArray(networkLinks) ? networkLinks : [];
    const linkById = new Map(links.map((item) => [item.link_id, item]));
    const assignments = new Map();
    const items = batch && Array.isArray(batch.items) ? batch.items : [];
    items.forEach((item) => {
      if (item && typeof item.sender_id === "string") assignments.set(item.sender_id, item);
    });

    const rows = [];
    SENDERS.forEach((senderId) => {
      const assignment = assignments.get(senderId) || null;
      TARGETS.forEach((target) => {
        const id = linkId(senderId, target);
        const used = target.id === "scheduler"
          ? assignment !== null
          : assignment !== null &&
            assignment.assignment_status === "ASSIGNED" &&
            assignment.edge_node_id === target.id;
        rows.push({
          linkId: id,
          senderId,
          targetId: target.id,
          protocol: target.protocol,
          used,
          assignment,
          network: linkById.get(id) || null,
        });
      });
    });
    return rows;
  }

  const api = { buildSenderLinkRows };
  root.TopologyLinks = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
