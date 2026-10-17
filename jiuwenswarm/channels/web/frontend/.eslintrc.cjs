module.exports = {
  root: true,
  env: { browser: true, es2020: true },
  parser: '@typescript-eslint/parser',
  parserOptions: { ecmaVersion: 'latest', sourceType: 'module' },
  plugins: ['@typescript-eslint', 'react-hooks'],
  extends: ['eslint:recommended', 'plugin:@typescript-eslint/recommended', 'plugin:react-hooks/recommended'],
  ignorePatterns: ['node_modules/', 'dist/', 'coverage/', '*.tsbuildinfo'],
  rules: {
    // TypeScript already enforces unused locals and parameters in tsconfig.json.
    '@typescript-eslint/no-unused-vars': 'off',
    // Existing RPC/extension boundaries use any; converting those contracts is a separate migration.
    '@typescript-eslint/no-explicit-any': 'off',
  },
  overrides: [
    {
      files: ['*.config.*', '*.cjs', 'scripts/**', 'tests/**'],
      env: { node: true },
    },
  ],
};
