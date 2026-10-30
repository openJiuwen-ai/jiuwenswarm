import { simulatedAuthProvider } from 'virtual:login-auth-simulate-provider';
import { managerAuthProvider } from './manager/ManagerAuthProvider';
import type { EnterpriseAuthProvider } from './types';

export function resolveEnterpriseAuthProvider(simulate: boolean): EnterpriseAuthProvider {
  if (!simulate) return managerAuthProvider;
  if (!simulatedAuthProvider) {
    throw new Error('LOGIN_AUTH_SIMULATE=true，但当前交付包未安装登录认证模拟插件');
  }
  return simulatedAuthProvider;
}
