export interface paths {
    "/api/v1/artifacts/{artifact_id}/content": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Get Artifact Content
         * @description 按登记形态读产物（spec §10、#222 §7）。
         *
         *     文本产物回纯文本（1 MiB 上限），媒体产物回字节并支持单段 Range——形态来自
         *     ``workbench_store`` 的登记表，这里不认文件名。``artifact_id`` 是
         *     ``<session_id>/<文件名>``：会话标识本身含斜杠，所以 ``:path`` 沿用会话路由的
         *     口径，最后一段才是文件名。越界与不存在一律 404 ``artifact_not_found``。
         */
        get: operations["get_artifact_content_api_v1_artifacts__artifact_id__content_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        /**
         * Head Artifact Content
         * @description 探测产物：与 GET 同头、无体（#222 §6.2 的 HEAD 一格同样适用于成片）。
         */
        head: operations["head_artifact_content_api_v1_artifacts__artifact_id__content_head"];
        patch?: never;
        trace?: never;
    };
    "/api/v1/auth/token": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Auth Token */
        get: operations["get_auth_token_api_v1_auth_token_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/health": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Health */
        get: operations["get_health_api_v1_health_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/media/{asset_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Get Media
         * @description 读素材字节；``Range`` 只支持单段（#222 §6.2）。
         */
        get: operations["get_media_api_v1_media__asset_id__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        /**
         * Head Media
         * @description 探测素材：头与 GET 一致、无体；供播放器决定要不要发 Range。
         */
        head: operations["head_media_api_v1_media__asset_id__head"];
        patch?: never;
        trace?: never;
    };
    "/api/v1/sessions": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Sessions */
        get: operations["list_sessions_api_v1_sessions_get"];
        put?: never;
        /** Create Session */
        post: operations["create_session_api_v1_sessions_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/sessions/{session_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Session */
        get: operations["get_session_api_v1_sessions__session_id__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/sessions/{session_id}/action-items": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * List Action Items
         * @description 会话的全部待办（含已决议的）：创建与决议成对可查，前端不必自己攒。
         */
        get: operations["list_action_items_api_v1_sessions__session_id__action_items_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/sessions/{session_id}/action-items/{item_id}/resolve": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Resolve Action Item
         * @description 提交人工决定：``approve`` 起阶段 2，``revise`` 从声明层次重跑阶段 1。
         *
         *     状态判定与「决议 + 起任务」的原子性都在 ``JobRunner.resolve`` 的会话临界区里，
         *     这里只把请求体归一成内部词汇，不先查一遍状态（两处判据会在并发下各说各话）。
         */
        post: operations["resolve_action_item_api_v1_sessions__session_id__action_items__item_id__resolve_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/sessions/{session_id}/artifacts": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * List Artifacts
         * @description 会话的产物清单：与详情里的 ``artifacts`` 同一份来源（目录现扫）。
         */
        get: operations["list_artifacts_api_v1_sessions__session_id__artifacts_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/sessions/{session_id}/assets": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * List Assets
         * @description 会话的素材清单：与详情里的 ``assets`` 同一份来源（目录现扫，无登记表）。
         */
        get: operations["list_assets_api_v1_sessions__session_id__assets_get"];
        put?: never;
        /**
         * Upload Asset
         * @description 收一份素材：边写边判超限、按字节定类型、按内容哈希去重（#222 §5）。
         *
         *     上传**不改会话状态、不产生事件**（D3-AC13）：素材是不可变的旁挂资源，与运行中的
         *     任务并行安全。
         */
        post: operations["upload_asset_api_v1_sessions__session_id__assets_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/sessions/{session_id}/events": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Stream Events
         * @description 订阅会话事件流。
         *
         *     **快照优先**（spec §8）：先 ``GET /api/v1/sessions/{session_id}`` 拿快照，再订阅
         *     本流；补不上缺口时按响应头的 ``X-Resync-Required`` 重取快照。
         */
        get: operations["stream_events_api_v1_sessions__session_id__events_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/sessions/{session_id}/jobs": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Start Job
         * @description 起一次 ``advance``：202 返回任务对象，执行在后台任务里。
         *
         *     闸门与单写入者判定在 ``JobRunner.advance`` 的会话临界区里完成；这里不再判一遍，
         *     否则「先查后写」的两处判据会在并发下各说各话（D2-AC3）。
         */
        post: operations["start_job_api_v1_sessions__session_id__jobs_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/sessions/{session_id}/jobs/{job_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Job */
        get: operations["get_job_api_v1_sessions__session_id__jobs__job_id__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/sessions/{session_id}/jobs/{job_id}/cancel": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Cancel Job
         * @description 请求取消：只打标记，任务在当前节点跑完后才停（**不承诺立即停止**）。
         */
        post: operations["cancel_job_api_v1_sessions__session_id__jobs__job_id__cancel_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
}
export type webhooks = Record<string, never>;
export interface components {
    schemas: {
        /** ActionItemOut */
        ActionItemOut: {
            /**
             * Created At
             * @default
             */
            created_at: string;
            /** Decision */
            decision?: string | null;
            /** Item Id */
            item_id: string;
            /** Kind */
            kind: string;
            /**
             * Resolved At
             * @default
             */
            resolved_at: string;
            /** Status */
            status: string;
        };
        /**
         * ActionItemResolve
         * @description 决议载荷（spec §7）：``approve`` 或 ``revise`` + 若干条（层次, 文字）。
         */
        ActionItemResolve: {
            /**
             * Decision
             * @enum {string}
             */
            decision: "approve" | "revise";
            /** Revisions */
            revisions?: components["schemas"]["RevisionIn"][];
        };
        /** ArtifactInfo */
        ArtifactInfo: {
            /** Artifact Id */
            artifact_id: string;
            /** Bytes */
            bytes: number;
            /** Created At */
            created_at: string;
            /** Kind */
            kind: string;
            /** Name */
            name: string;
        };
        /**
         * Asset
         * @description 上传素材的对外形状（#222 §9）。``asset_id`` 与 ``artifact_id`` 同形：
         *     ``<session_id>/<sha256 前 16 位>``——路径参数按 ``{asset_id:path}`` 承接。
         */
        Asset: {
            /** Asset Id */
            asset_id: string;
            /** Bytes */
            bytes: number;
            /** Created At */
            created_at: string;
            /** Kind */
            kind: string;
            /** Name */
            name: string;
        };
        /**
         * AuthTokenResponse
         * @description 启动令牌资源：字段名即契约，前端据此缓存到内存。
         */
        AuthTokenResponse: {
            /** Token */
            token: string;
        };
        /** BriefOut */
        BriefOut: {
            /** Canvases */
            canvases?: {
                [key: string]: number[];
            };
            /**
             * Clarifications
             * @default
             */
            clarifications: string;
            /**
             * Director Mode
             * @default
             */
            director_mode: string;
            /**
             * Draft
             * @default
             */
            draft: string;
            /** Duration */
            duration: number;
            /** Language */
            language: string;
            /** Mode */
            mode: string;
            /** Plot */
            plot: string;
            /** Refs */
            refs?: components["schemas"]["RefOut"][];
            /** Style */
            style: string;
            /** Variant */
            variant: string;
            video_output?: components["schemas"]["VideoOutputOut"] | null;
        };
        /**
         * HealthResponse
         * @description 健康状态资源。字段约定见 D1 spec §7.1。
         */
        HealthResponse: {
            /** Service */
            service: string;
            /**
             * Status
             * @constant
             */
            status: "ok";
            /** Version */
            version: string;
        };
        /** JobSummary */
        JobSummary: {
            /**
             * Cancel Requested
             * @default false
             */
            cancel_requested: boolean;
            /** Cancelled At Node */
            cancelled_at_node?: string | null;
            /** Error Code */
            error_code?: string | null;
            /**
             * Finished At
             * @default
             */
            finished_at: string;
            /** Job Id */
            job_id: string;
            /**
             * Kind
             * @default advance
             */
            kind: string;
            /**
             * Started At
             * @default
             */
            started_at: string;
            /** Status */
            status: string;
        };
        /**
         * ProblemDetail
         * @description 统一错误资源（RFC 9457）。
         */
        ProblemDetail: {
            /** Code */
            code?: string | null;
            /** Detail */
            detail: string;
            /** Errors */
            errors?: components["schemas"]["ProblemFieldError"][] | null;
            /** Instance */
            instance: string;
            /** Status */
            status: number;
            /** Title */
            title: string;
            /**
             * Type
             * @default about:blank
             */
            type: string;
        };
        /**
         * ProblemFieldError
         * @description 422 校验错误元素：``{loc, msg, type}``。
         */
        ProblemFieldError: {
            /** Loc */
            loc: (string | number)[];
            /** Msg */
            msg: string;
            /** Type */
            type: string;
        };
        /** RefIn */
        RefIn: {
            /**
             * Description
             * @default
             */
            description: string;
            /**
             * Name
             * @default
             */
            name: string;
            /**
             * Path
             * @default
             */
            path: string;
            /**
             * Picture
             * @default 1
             */
            picture: number;
        };
        /** RefOut */
        RefOut: {
            /**
             * Description
             * @default
             */
            description: string;
            /**
             * Name
             * @default
             */
            name: string;
            /**
             * Path
             * @default
             */
            path: string;
            /** Picture */
            picture: number;
        };
        /** RevisionIn */
        RevisionIn: {
            /** Layer */
            layer: string;
            /** Text */
            text: string;
        };
        /**
         * SessionCreate
         * @description 建会话的 brief 一次性给全（spec §7）。
         *
         *     D2 只做 ``base`` 短视频，所以 ``mode`` 是单值字面量；扩大路线由后续票改契约，
         *     不在这里留一个「看起来支持」的自由字符串。
         */
        SessionCreate: {
            /** Canvases */
            canvases?: {
                [key: string]: [
                    number,
                    number
                ];
            };
            /**
             * Clarifications
             * @default
             */
            clarifications: string;
            /**
             * Draft
             * @default
             */
            draft: string;
            /**
             * Duration
             * @default 4
             */
            duration: number;
            /**
             * Language
             * @default Chinese
             */
            language: string;
            /**
             * Mode
             * @default base
             * @constant
             */
            mode: "base";
            /** Plot */
            plot: string;
            /** Refs */
            refs?: components["schemas"]["RefIn"][];
            /**
             * Style
             * @default Cinematic
             */
            style: string;
            /**
             * Variant
             * @default T2VA
             */
            variant: string;
            video_output?: components["schemas"]["VideoOutputIn"] | null;
        };
        /** SessionDetail */
        SessionDetail: {
            /** Action Items */
            action_items?: components["schemas"]["ActionItemOut"][];
            /** Artifacts */
            artifacts?: components["schemas"]["ArtifactInfo"][];
            /** Assets */
            assets?: components["schemas"]["Asset"][];
            brief: components["schemas"]["BriefOut"];
            /** Created At */
            created_at: string;
            current_job?: components["schemas"]["JobSummary"] | null;
            /** Jobs */
            jobs?: components["schemas"]["JobSummary"][];
            /** Last Event Seq */
            last_event_seq: number;
            /** Legacy */
            legacy: boolean;
            /** Mode */
            mode: string;
            /** Plot */
            plot: string;
            /** Revision */
            revision: number;
            /** Session Id */
            session_id: string;
            /**
             * Status
             * @enum {string}
             */
            status: "created" | "running" | "awaiting_human" | "completed" | "failed" | "cancelled" | "interrupted";
            /** Updated At */
            updated_at: string;
            /** Variant */
            variant: string;
        };
        /** SessionPage */
        SessionPage: {
            /** Items */
            items: components["schemas"]["SessionSummary"][];
            /** Next Cursor */
            next_cursor?: string | null;
        };
        /** SessionSummary */
        SessionSummary: {
            /** Created At */
            created_at: string;
            /** Legacy */
            legacy: boolean;
            /** Mode */
            mode: string;
            /** Plot */
            plot: string;
            /** Revision */
            revision: number;
            /** Session Id */
            session_id: string;
            /**
             * Status
             * @enum {string}
             */
            status: "created" | "running" | "awaiting_human" | "completed" | "failed" | "cancelled" | "interrupted";
            /** Updated At */
            updated_at: string;
            /** Variant */
            variant: string;
        };
        /** VideoOutputIn */
        VideoOutputIn: {
            /** Megapixels */
            megapixels: number;
            /** Multiple */
            multiple: number;
            /** Ratio */
            ratio: string;
        };
        /** VideoOutputOut */
        VideoOutputOut: {
            /** Megapixels */
            megapixels: number;
            /** Multiple */
            multiple: number;
            /** Ratio */
            ratio: string;
        };
    };
    responses: never;
    parameters: never;
    requestBodies: never;
    headers: never;
    pathItems: never;
}
export type $defs = Record<string, never>;
export interface operations {
    get_artifact_content_api_v1_artifacts__artifact_id__content_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                artifact_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description 文本产物全文（UTF-8，1 MiB 上限）或媒体产物的整份字节。 */
            200: {
                headers: {
                    /** @description 恒为 bytes（单段） */
                    "Accept-Ranges"?: string;
                    /** @description inline；显示名按 RFC 5987 编码 */
                    "Content-Disposition"?: string;
                    /** @description 本次响应的字节数（HEAD 时为 GET 会给的字节数） */
                    "Content-Length"?: number;
                    /** @description 缓存标识（素材即内容哈希） */
                    ETag?: string;
                    /** @description 文件修改时间（HTTP-date） */
                    "Last-Modified"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "text/plain": string;
                    "video/mp4": string;
                };
            };
            /** @description 媒体产物的单段字节范围（Range: bytes=…）。 */
            206: {
                headers: {
                    /** @description 恒为 bytes（单段） */
                    "Accept-Ranges"?: string;
                    /** @description inline；显示名按 RFC 5987 编码 */
                    "Content-Disposition"?: string;
                    /** @description 本次响应的字节数（HEAD 时为 GET 会给的字节数） */
                    "Content-Length"?: number;
                    /** @description bytes <start>-<end>/<len> */
                    "Content-Range"?: string;
                    /** @description 缓存标识（素材即内容哈希） */
                    ETag?: string;
                    /** @description 文件修改时间（HTTP-date） */
                    "Last-Modified"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "video/mp4": string;
                };
            };
            /** @description 缺少或无效的工作台访问令牌（problem+json） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description Host/Origin 不是本机回环地址（problem+json） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 产物不存在、标识越界，或该名字不在登记范围内（problem+json） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 会话状态冲突（problem+json） */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 文本产物超过 1 MiB（problem+json） */
            413: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 媒体产物的 Range 越界、语法非法或多段（problem+json）；带 Content-Range: bytes *\/<len> */
            416: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 请求参数校验失败（problem+json） */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 服务器内部错误（problem+json） */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
        };
    };
    head_artifact_content_api_v1_artifacts__artifact_id__content_head: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                artifact_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description 与 GET 同头、无体。 */
            200: {
                headers: {
                    /** @description 恒为 bytes（单段） */
                    "Accept-Ranges"?: string;
                    /** @description inline；显示名按 RFC 5987 编码 */
                    "Content-Disposition"?: string;
                    /** @description 本次响应的字节数（HEAD 时为 GET 会给的字节数） */
                    "Content-Length"?: number;
                    /** @description 缓存标识（素材即内容哈希） */
                    ETag?: string;
                    /** @description 文件修改时间（HTTP-date） */
                    "Last-Modified"?: string;
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description 媒体产物的字节范围，与 GET 同头、无体。 */
            206: {
                headers: {
                    /** @description 恒为 bytes（单段） */
                    "Accept-Ranges"?: string;
                    /** @description inline；显示名按 RFC 5987 编码 */
                    "Content-Disposition"?: string;
                    /** @description 本次响应的字节数（HEAD 时为 GET 会给的字节数） */
                    "Content-Length"?: number;
                    /** @description bytes <start>-<end>/<len> */
                    "Content-Range"?: string;
                    /** @description 缓存标识（素材即内容哈希） */
                    ETag?: string;
                    /** @description 文件修改时间（HTTP-date） */
                    "Last-Modified"?: string;
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description 缺少或无效的工作台访问令牌（problem+json） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description Host/Origin 不是本机回环地址（problem+json） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 产物不存在、标识越界，或该名字不在登记范围内（problem+json） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 会话状态冲突（problem+json） */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 文本产物超过 1 MiB（problem+json） */
            413: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 媒体产物的 Range 越界、语法非法或多段（problem+json）；带 Content-Range: bytes *\/<len> */
            416: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 请求参数校验失败（problem+json） */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 服务器内部错误（problem+json） */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
        };
    };
    get_auth_token_api_v1_auth_token_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AuthTokenResponse"];
                };
            };
            /** @description 资源不存在（problem+json） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 请求参数校验失败（problem+json） */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 服务器内部错误（problem+json） */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
        };
    };
    get_health_api_v1_health_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HealthResponse"];
                };
            };
            /** @description 资源不存在（problem+json） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 请求参数校验失败（problem+json） */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 服务器内部错误（problem+json） */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
        };
    };
    get_media_api_v1_media__asset_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                asset_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description 整份字节；Content-Type 由素材的实际格式决定。 */
            200: {
                headers: {
                    /** @description 恒为 bytes（单段） */
                    "Accept-Ranges"?: string;
                    /** @description inline；显示名按 RFC 5987 编码 */
                    "Content-Disposition"?: string;
                    /** @description 本次响应的字节数（HEAD 时为 GET 会给的字节数） */
                    "Content-Length"?: number;
                    /** @description 缓存标识（素材即内容哈希） */
                    ETag?: string;
                    /** @description 文件修改时间（HTTP-date） */
                    "Last-Modified"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "audio/mpeg": string;
                    "audio/wav": string;
                    "image/jpeg": string;
                    "image/png": string;
                    "image/webp": string;
                    "video/mp4": string;
                    "video/webm": string;
                };
            };
            /** @description 单段字节范围（Range: bytes=…）。 */
            206: {
                headers: {
                    /** @description 恒为 bytes（单段） */
                    "Accept-Ranges"?: string;
                    /** @description inline；显示名按 RFC 5987 编码 */
                    "Content-Disposition"?: string;
                    /** @description 本次响应的字节数（HEAD 时为 GET 会给的字节数） */
                    "Content-Length"?: number;
                    /** @description bytes <start>-<end>/<len> */
                    "Content-Range"?: string;
                    /** @description 缓存标识（素材即内容哈希） */
                    ETag?: string;
                    /** @description 文件修改时间（HTTP-date） */
                    "Last-Modified"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "audio/mpeg": string;
                    "audio/wav": string;
                    "image/jpeg": string;
                    "image/png": string;
                    "image/webp": string;
                    "video/mp4": string;
                    "video/webm": string;
                };
            };
            /** @description 缺少或无效的工作台访问令牌（problem+json） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description Host/Origin 不是本机回环地址（problem+json） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 素材不存在、标识越界，或不是素材命名形态（problem+json） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description Range 越界、语法非法或多段（problem+json）；带 Content-Range: bytes *\/<len> */
            416: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 请求参数校验失败（problem+json） */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 服务器内部错误（problem+json） */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
        };
    };
    head_media_api_v1_media__asset_id__head: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                asset_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description 与 GET 同头、无体。 */
            200: {
                headers: {
                    /** @description 恒为 bytes（单段） */
                    "Accept-Ranges"?: string;
                    /** @description inline；显示名按 RFC 5987 编码 */
                    "Content-Disposition"?: string;
                    /** @description 本次响应的字节数（HEAD 时为 GET 会给的字节数） */
                    "Content-Length"?: number;
                    /** @description 缓存标识（素材即内容哈希） */
                    ETag?: string;
                    /** @description 文件修改时间（HTTP-date） */
                    "Last-Modified"?: string;
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description 与 GET 同头、无体。 */
            206: {
                headers: {
                    /** @description 恒为 bytes（单段） */
                    "Accept-Ranges"?: string;
                    /** @description inline；显示名按 RFC 5987 编码 */
                    "Content-Disposition"?: string;
                    /** @description 本次响应的字节数（HEAD 时为 GET 会给的字节数） */
                    "Content-Length"?: number;
                    /** @description bytes <start>-<end>/<len> */
                    "Content-Range"?: string;
                    /** @description 缓存标识（素材即内容哈希） */
                    ETag?: string;
                    /** @description 文件修改时间（HTTP-date） */
                    "Last-Modified"?: string;
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description 缺少或无效的工作台访问令牌（problem+json） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description Host/Origin 不是本机回环地址（problem+json） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 素材不存在、标识越界，或不是素材命名形态（problem+json） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description Range 越界、语法非法或多段（problem+json）；带 Content-Range: bytes *\/<len> */
            416: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 请求参数校验失败（problem+json） */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 服务器内部错误（problem+json） */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
        };
    };
    list_sessions_api_v1_sessions_get: {
        parameters: {
            query?: {
                limit?: number;
                cursor?: string | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SessionPage"];
                };
            };
            /** @description 缺少或无效的工作台访问令牌（problem+json） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description Host/Origin 不是本机回环地址（problem+json） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 资源不存在（problem+json） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 会话状态冲突（problem+json） */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 请求参数校验失败（problem+json） */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 服务器内部错误（problem+json） */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
        };
    };
    create_session_api_v1_sessions_post: {
        parameters: {
            query?: never;
            header?: {
                "Idempotency-Key"?: string | null;
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["SessionCreate"];
            };
        };
        responses: {
            /** @description 幂等重放：返回第一次的结果 */
            200: {
                headers: {
                    /** @description 重放标记；首次创建不返回该头 */
                    "Idempotency-Replayed"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SessionDetail"];
                };
            };
            /** @description Successful Response */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SessionDetail"];
                };
            };
            /** @description 缺少或无效的工作台访问令牌（problem+json） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description Host/Origin 不是本机回环地址（problem+json） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 资源不存在（problem+json） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 会话状态冲突（problem+json） */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 请求参数校验失败（problem+json） */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 服务器内部错误（problem+json） */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
        };
    };
    get_session_api_v1_sessions__session_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                session_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SessionDetail"];
                };
            };
            /** @description 缺少或无效的工作台访问令牌（problem+json） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description Host/Origin 不是本机回环地址（problem+json） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 资源不存在（problem+json） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 会话状态冲突（problem+json） */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 请求参数校验失败（problem+json） */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 服务器内部错误（problem+json） */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
        };
    };
    list_action_items_api_v1_sessions__session_id__action_items_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                session_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ActionItemOut"][];
                };
            };
            /** @description 缺少或无效的工作台访问令牌（problem+json） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description Host/Origin 不是本机回环地址（problem+json） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 资源不存在（problem+json） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 会话状态冲突（problem+json） */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 请求参数校验失败（problem+json） */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 服务器内部错误（problem+json） */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
        };
    };
    resolve_action_item_api_v1_sessions__session_id__action_items__item_id__resolve_post: {
        parameters: {
            query?: never;
            header?: {
                "Idempotency-Key"?: string | null;
            };
            path: {
                session_id: string;
                item_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ActionItemResolve"];
            };
        };
        responses: {
            /** @description 决议已受理，重跑在后台执行（异步） */
            202: {
                headers: {
                    /** @description 重放标记；首次决议不返回该头 */
                    "Idempotency-Replayed"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["JobSummary"];
                };
            };
            /** @description 缺少或无效的工作台访问令牌（problem+json） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description Host/Origin 不是本机回环地址（problem+json） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 待办不存在（problem+json） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 会话/待办状态冲突（problem+json） */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 请求参数校验失败（problem+json） */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 服务器内部错误（problem+json） */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
        };
    };
    list_artifacts_api_v1_sessions__session_id__artifacts_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                session_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ArtifactInfo"][];
                };
            };
            /** @description 缺少或无效的工作台访问令牌（problem+json） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description Host/Origin 不是本机回环地址（problem+json） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 资源不存在（problem+json） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 会话状态冲突（problem+json） */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 请求参数校验失败（problem+json） */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 服务器内部错误（problem+json） */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
        };
    };
    list_assets_api_v1_sessions__session_id__assets_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                session_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Asset"][];
                };
            };
            /** @description 缺少或无效的工作台访问令牌（problem+json） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description Host/Origin 不是本机回环地址（problem+json） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 资源不存在（problem+json） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 请求参数校验失败（problem+json） */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 服务器内部错误（problem+json） */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
        };
    };
    upload_asset_api_v1_sessions__session_id__assets_post: {
        parameters: {
            query?: never;
            header?: {
                "Idempotency-Key"?: string | null;
            };
            path: {
                session_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "multipart/form-data": {
                    /** Format: binary */
                    file: string;
                };
            };
        };
        responses: {
            /** @description 幂等重放：返回第一次写入的素材 */
            200: {
                headers: {
                    /** @description 重放标记；首次上传不返回该头 */
                    "Idempotency-Replayed"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Asset"];
                };
            };
            /** @description Successful Response */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Asset"];
                };
            };
            /** @description 请求不是可解析的 multipart 表单，或缺 file 字段（problem+json） */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 缺少或无效的工作台访问令牌（problem+json） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description Host/Origin 不是本机回环地址（problem+json） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 资源不存在（problem+json） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 历史会话只读，或幂等键已用于另一份内容（problem+json） */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 超过该类型上限，已中止写入（problem+json） */
            413: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 魔数不在白名单，或声明的扩展名与内容不符（problem+json） */
            415: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 请求参数校验失败（problem+json） */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 服务器内部错误（problem+json） */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 磁盘写入失败（problem+json） */
            507: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
        };
    };
    stream_events_api_v1_sessions__session_id__events_get: {
        parameters: {
            query?: {
                last_event_id?: string | null;
            };
            header?: {
                "Last-Event-ID"?: string | null;
            };
            path: {
                session_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description 事件流（SSE）。快照优先：先 GET 详情拿快照，再订阅本流；断线重连带 Last-Event-ID（或 ?last_event_id=）补发序号更大的事件；序号超出事件流尾号时响应头带 X-Resync-Required: true，要求客户端重取快照。每 15 秒一条心跳注释行；同一会话允许多条并发订阅。 */
            200: {
                headers: {
                    /** @description 缺口补不上，需要重新 GET 详情拿快照 */
                    "X-Resync-Required"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "text/event-stream": string;
                };
            };
            /** @description 缺少或无效的工作台访问令牌（problem+json） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description Host/Origin 不是本机回环地址（problem+json） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 资源不存在（problem+json） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 会话状态冲突（problem+json） */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description Last-Event-ID 不是事件序号（problem+json） */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 服务器内部错误（problem+json） */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
        };
    };
    start_job_api_v1_sessions__session_id__jobs_post: {
        parameters: {
            query?: never;
            header?: {
                "Idempotency-Key"?: string | null;
            };
            path: {
                session_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description 任务已受理（异步执行） */
            202: {
                headers: {
                    /** @description 重放标记；首次起任务不返回该头 */
                    "Idempotency-Replayed"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["JobSummary"];
                };
            };
            /** @description 缺少或无效的工作台访问令牌（problem+json） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description Host/Origin 不是本机回环地址（problem+json） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 任务不存在（problem+json） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 会话/任务状态冲突（problem+json） */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 请求参数校验失败（problem+json） */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 服务器内部错误（problem+json） */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
        };
    };
    get_job_api_v1_sessions__session_id__jobs__job_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                session_id: string;
                job_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["JobSummary"];
                };
            };
            /** @description 缺少或无效的工作台访问令牌（problem+json） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description Host/Origin 不是本机回环地址（problem+json） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 任务不存在（problem+json） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 会话/任务状态冲突（problem+json） */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 请求参数校验失败（problem+json） */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 服务器内部错误（problem+json） */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
        };
    };
    cancel_job_api_v1_sessions__session_id__jobs__job_id__cancel_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                session_id: string;
                job_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["JobSummary"];
                };
            };
            /** @description 缺少或无效的工作台访问令牌（problem+json） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description Host/Origin 不是本机回环地址（problem+json） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 任务不存在（problem+json） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 会话/任务状态冲突（problem+json） */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 请求参数校验失败（problem+json） */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 服务器内部错误（problem+json） */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
        };
    };
}
