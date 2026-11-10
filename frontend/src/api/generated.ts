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
         * @description 读文本产物全文（spec §10）。
         *
         *     ``artifact_id`` 是 ``<session_id>/<文件名>``：会话标识本身含斜杠，所以 ``:path``
         *     沿用会话路由的口径，最后一段才是文件名。越界与不存在一律 404 ``artifact_not_found``。
         */
        get: operations["get_artifact_content_api_v1_artifacts__artifact_id__content_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
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
            /** @description 文本产物全文（UTF-8）；视频类在 D2 不提供内容。 */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "text/plain": string;
                };
            };
            /** @description 产物不存在、标识越界，或该产物不提供内容（problem+json） */
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
