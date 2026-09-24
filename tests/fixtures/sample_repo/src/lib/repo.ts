import { db } from "@/server/db";
import { getUser } from "@/server/users";

export interface IRepo<T> {
  find(id: string): Promise<T | null>;
}

export abstract class BaseRepo {
  protected readonly client = db;
}

/** Repository wrapper around user queries. */
export class UserRepo extends BaseRepo implements IRepo<unknown> {
  async find(id: string) {
    return getUser(id);
  }
}

export enum Role {
  USER = "USER",
  ADMIN = "ADMIN",
}

export const MAX_PAGE_SIZE = 50;
