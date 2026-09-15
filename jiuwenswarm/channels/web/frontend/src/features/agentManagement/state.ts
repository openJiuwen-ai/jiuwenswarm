import type {
  AgentCatalogItem,
  AgentDetail,
  AgentFileContent,
  DefinitionFileEntry,
  RequestStatus,
  SkillOption,
} from './types';

export type AgentManagementState = {
  catalog: AgentCatalogItem[];
  catalogStatus: RequestStatus;
  catalogError: string | null;
  detail: AgentDetail | null;
  detailStatus: RequestStatus;
  detailError: string | null;
  files: DefinitionFileEntry[];
  filesStatus: RequestStatus;
  filesError: string | null;
  selectedFilePath: string | null;
  fileContent: AgentFileContent | null;
  fileStatus: RequestStatus;
  fileError: string | null;
  skillOptions: SkillOption[];
  skillsStatus: RequestStatus;
};

export function createInitialAgentManagementState(): AgentManagementState {
  return {
    catalog: [],
    catalogStatus: 'idle',
    catalogError: null,
    detail: null,
    detailStatus: 'idle',
    detailError: null,
    files: [],
    filesStatus: 'idle',
    filesError: null,
    selectedFilePath: null,
    fileContent: null,
    fileStatus: 'idle',
    fileError: null,
    skillOptions: [],
    skillsStatus: 'idle',
  };
}

export const initialAgentManagementState = createInitialAgentManagementState();

export type AgentManagementAction =
  | { type: 'catalog.loading' }
  | { type: 'catalog.loaded'; catalog: AgentCatalogItem[] }
  | { type: 'catalog.error'; message: string }
  | { type: 'detail.loading' }
  | { type: 'detail.loaded'; detail: AgentDetail }
  | { type: 'detail.error'; message: string }
  | { type: 'files.loading' }
  | { type: 'files.loaded'; files: DefinitionFileEntry[] }
  | { type: 'files.error'; message: string }
  | { type: 'file.loading'; relativePath: string }
  | { type: 'file.unsupported'; relativePath: string }
  | { type: 'file.loaded'; content: AgentFileContent }
  | { type: 'file.error'; message: string }
  | { type: 'skills.loading' }
  | { type: 'skills.loaded'; options: SkillOption[] }
  | { type: 'skills.error' };

export function agentManagementReducer(
  state: AgentManagementState,
  action: AgentManagementAction,
): AgentManagementState {
  switch (action.type) {
    case 'catalog.loading':
      return { ...state, catalogStatus: 'loading', catalogError: null };
    case 'catalog.loaded':
      return { ...state, catalog: action.catalog, catalogStatus: 'success', catalogError: null };
    case 'catalog.error':
      return { ...state, catalog: [], catalogStatus: 'error', catalogError: action.message };
    case 'detail.loading':
      return {
        ...state,
        detail: null,
        detailStatus: 'loading',
        detailError: null,
        files: [],
        filesStatus: 'idle',
        filesError: null,
        selectedFilePath: null,
        fileContent: null,
        fileStatus: 'idle',
        fileError: null,
      };
    case 'detail.loaded':
      return { ...state, detail: action.detail, detailStatus: 'success', detailError: null };
    case 'detail.error':
      return { ...state, detailStatus: 'error', detailError: action.message };
    case 'files.loading':
      return { ...state, filesStatus: 'loading', filesError: null, fileContent: null, fileStatus: 'idle' };
    case 'files.loaded':
      return { ...state, files: action.files, filesStatus: 'success', filesError: null };
    case 'files.error':
      return { ...state, filesStatus: 'error', filesError: action.message };
    case 'file.loading':
      return {
        ...state,
        selectedFilePath: action.relativePath,
        fileContent: null,
        fileStatus: 'loading',
        fileError: null,
      };
    case 'file.unsupported':
      return {
        ...state,
        selectedFilePath: action.relativePath,
        fileContent: null,
        fileStatus: 'success',
        fileError: null,
      };
    case 'file.loaded':
      return {
        ...state,
        selectedFilePath: action.content.relativePath,
        fileContent: action.content,
        fileStatus: 'success',
        fileError: null,
      };
    case 'file.error':
      return { ...state, fileStatus: 'error', fileError: action.message };
    case 'skills.loading':
      return { ...state, skillsStatus: 'loading' };
    case 'skills.loaded':
      return { ...state, skillOptions: action.options, skillsStatus: 'success' };
    case 'skills.error':
      return { ...state, skillsStatus: 'error' };
    default:
      return state;
  }
}
