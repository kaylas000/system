import { PrismaClient } from "@prisma/client";

/** Shared Prisma client (one instance per process). */
export const db = new PrismaClient();
