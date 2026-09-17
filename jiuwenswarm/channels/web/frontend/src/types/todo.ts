/**
 * Todo 类型定义
 */

export type TodoStatus = 'pending' | 'in_progress' | 'completed';

export interface TodoItem {
  id: string;
  content: string;
  activeForm: string;
  status: TodoStatus;
  createdAt: string;
  updatedAt: string;
  depends?: string[];
  claimedBy?: string;
}
