import { createHash, randomBytes } from "node:crypto";
import { and, eq } from "drizzle-orm";
import type { Db } from "@paperclipai/db";
import { agentApiKeys, agents, joinRequests } from "@paperclipai/db";
import type { HireApprovedPayload } from "@paperclipai/adapter-utils";
import { findActiveServerAdapter } from "../adapters/registry.js";
import { logger } from "../middleware/logger.js";
import { logActivity } from "./activity-log.js";

const HIRE_APPROVED_MESSAGE =
  "Tell your user that your hire was approved, now they should assign you a task in Paperclip or ask you to create issues.";

export interface NotifyHireApprovedInput {
  companyId: string;
  agentId: string;
  source: "join_request" | "approval";
  sourceId: string;
  approvedAt?: Date;
}

/**
 * Invokes the adapter's onHireApproved hook when an agent is approved (join-request or hire_agent approval).
 * Failures are non-fatal: we log and write to activity, never throw.
 */
export async function notifyHireApproved(
  db: Db,
  input: NotifyHireApprovedInput,
): Promise<void> {
  const { companyId, agentId, source, sourceId } = input;
  const approvedAt = input.approvedAt ?? new Date();

  const row = await db
    .select()
    .from(agents)
    .where(and(eq(agents.id, agentId), eq(agents.companyId, companyId)))
    .then((rows) => rows[0] ?? null);

  if (!row) {
    logger.warn({ companyId, agentId, source, sourceId }, "hire hook: agent not found in company, skipping");
    return;
  }

  const adapterType = row.adapterType ?? "process";
  const adapter = findActiveServerAdapter(adapterType);
  const onHireApproved = adapter?.onHireApproved;
  if (!onHireApproved) {
    return;
  }

  // For join_request approvals, generate a fresh claim secret if the API key
  // has not been claimed yet so the adapter can autonomously trigger claiming.
  let freshClaimSecret: string | undefined;
  let joinRequestId: string | undefined;
  if (source === "join_request") {
    try {
      const joinRequest = await db
        .select()
        .from(joinRequests)
        .where(eq(joinRequests.id, sourceId))
        .then((rows) => rows[0] ?? null);

      if (joinRequest?.createdAgentId && !joinRequest.claimSecretConsumedAt) {
        const existingKey = await db
          .select({ id: agentApiKeys.id })
          .from(agentApiKeys)
          .where(eq(agentApiKeys.agentId, joinRequest.createdAgentId))
          .then((rows) => rows[0] ?? null);

        if (!existingKey) {
          const secret = `pcp_claim_${randomBytes(24).toString("hex")}`;
          const secretHash = createHash("sha256").update(secret).digest("hex");
          await db
            .update(joinRequests)
            .set({
              claimSecretHash: secretHash,
              claimSecretExpiresAt: new Date(Date.now() + 7 * 24 * 60 * 60 * 1000),
              updatedAt: new Date(),
            })
            .where(eq(joinRequests.id, sourceId));
          freshClaimSecret = secret;
          joinRequestId = sourceId;
        }
      }
    } catch (err) {
      logger.warn(
        { err, companyId, agentId, sourceId },
        "hire hook: failed to generate fresh claim secret, adapter will receive no freshClaimSecret",
      );
    }
  }

  const payload: HireApprovedPayload = {
    companyId,
    agentId,
    agentName: row.name,
    adapterType,
    source,
    sourceId,
    approvedAt: approvedAt.toISOString(),
    message: HIRE_APPROVED_MESSAGE,
    ...(freshClaimSecret !== undefined && { freshClaimSecret }),
    ...(joinRequestId !== undefined && { joinRequestId }),
  };

  const adapterConfig =
    typeof row.adapterConfig === "object" && row.adapterConfig !== null && !Array.isArray(row.adapterConfig)
      ? (row.adapterConfig as Record<string, unknown>)
      : {};

  try {
    const result = await onHireApproved(payload, adapterConfig);
    if (result.ok) {
      await logActivity(db, {
        companyId,
        actorType: "system",
        actorId: "hire_hook",
        action: "hire_hook.succeeded",
        entityType: "agent",
        entityId: agentId,
        details: { source, sourceId, adapterType },
      });
      return;
    }

    logger.warn(
      { companyId, agentId, adapterType, source, sourceId, error: result.error, detail: result.detail },
      "hire hook: adapter returned failure",
    );
    await logActivity(db, {
      companyId,
      actorType: "system",
      actorId: "hire_hook",
      action: "hire_hook.failed",
      entityType: "agent",
      entityId: agentId,
      details: { source, sourceId, adapterType, error: result.error, detail: result.detail },
    });
  } catch (err) {
    logger.error(
      { err, companyId, agentId, adapterType, source, sourceId },
      "hire hook: adapter threw",
    );
    await logActivity(db, {
      companyId,
      actorType: "system",
      actorId: "hire_hook",
      action: "hire_hook.error",
      entityType: "agent",
      entityId: agentId,
      details: {
        source,
        sourceId,
        adapterType,
        error: err instanceof Error ? err.message : String(err),
      },
    });
  }
}
