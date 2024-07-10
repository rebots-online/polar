/* tslint:disable */
/* eslint-disable */
/**
 * Polar API
 * Read the docs at https://docs.polar.sh/api
 *
 * The version of the OpenAPI document: 0.1.0
 * 
 *
 * NOTE: This class is auto generated by OpenAPI Generator (https://openapi-generator.tech).
 * https://openapi-generator.tech
 * Do not edit the class manually.
 */


import * as runtime from '../runtime';
import type {
  HTTPValidationError,
  ListResourceSubscription,
  OrganizationIDFilter,
  OrganizationId,
  ProductIDFilter,
  Subscription,
  SubscriptionCreateEmail,
  SubscriptionTierTypeFilter,
  SubscriptionsImported,
} from '../models/index';

export interface SubscriptionsApiCreateRequest {
    body: SubscriptionCreateEmail;
}

export interface SubscriptionsApiExportRequest {
    organizationId?: OrganizationId;
}

export interface SubscriptionsApiImportRequest {
    file: Blob;
    organizationId: string;
}

export interface SubscriptionsApiListRequest {
    organizationId?: OrganizationIDFilter;
    productId?: ProductIDFilter;
    type?: SubscriptionTierTypeFilter;
    active?: boolean;
    page?: number;
    limit?: number;
    sorting?: Array<string>;
}

/**
 * 
 */
export class SubscriptionsApi extends runtime.BaseAPI {

    /**
     * Create a subscription on the free tier for a given email.
     * Create Free Subscription
     */
    async createRaw(requestParameters: SubscriptionsApiCreateRequest, initOverrides?: RequestInit | runtime.InitOverrideFunction): Promise<runtime.ApiResponse<Subscription>> {
        if (requestParameters['body'] == null) {
            throw new runtime.RequiredError(
                'body',
                'Required parameter "body" was null or undefined when calling create().'
            );
        }

        const queryParameters: any = {};

        const headerParameters: runtime.HTTPHeaders = {};

        headerParameters['Content-Type'] = 'application/json';

        if (this.configuration && this.configuration.accessToken) {
            const token = this.configuration.accessToken;
            const tokenString = await token("HTTPBearer", []);

            if (tokenString) {
                headerParameters["Authorization"] = `Bearer ${tokenString}`;
            }
        }
        const response = await this.request({
            path: `/v1/subscriptions/`,
            method: 'POST',
            headers: headerParameters,
            query: queryParameters,
            body: requestParameters['body'],
        }, initOverrides);

        return new runtime.JSONApiResponse(response);
    }

    /**
     * Create a subscription on the free tier for a given email.
     * Create Free Subscription
     */
    async create(requestParameters: SubscriptionsApiCreateRequest, initOverrides?: RequestInit | runtime.InitOverrideFunction): Promise<Subscription> {
        const response = await this.createRaw(requestParameters, initOverrides);
        return await response.value();
    }

    /**
     * Export subscriptions as a CSV file.
     * Export Subscriptions
     */
    async exportRaw(requestParameters: SubscriptionsApiExportRequest, initOverrides?: RequestInit | runtime.InitOverrideFunction): Promise<runtime.ApiResponse<any>> {
        const queryParameters: any = {};

        if (requestParameters['organizationId'] != null) {
            queryParameters['organization_id'] = requestParameters['organizationId'];
        }

        const headerParameters: runtime.HTTPHeaders = {};

        if (this.configuration && this.configuration.accessToken) {
            const token = this.configuration.accessToken;
            const tokenString = await token("HTTPBearer", []);

            if (tokenString) {
                headerParameters["Authorization"] = `Bearer ${tokenString}`;
            }
        }
        const response = await this.request({
            path: `/v1/subscriptions/export`,
            method: 'GET',
            headers: headerParameters,
            query: queryParameters,
        }, initOverrides);

        if (this.isJsonMime(response.headers.get('content-type'))) {
            return new runtime.JSONApiResponse<any>(response);
        } else {
            return new runtime.TextApiResponse(response) as any;
        }
    }

    /**
     * Export subscriptions as a CSV file.
     * Export Subscriptions
     */
    async export(requestParameters: SubscriptionsApiExportRequest = {}, initOverrides?: RequestInit | runtime.InitOverrideFunction): Promise<any> {
        const response = await this.exportRaw(requestParameters, initOverrides);
        return await response.value();
    }

    /**
     * Import subscriptions from a CSV file.
     * Import Subscriptions
     */
    async importRaw(requestParameters: SubscriptionsApiImportRequest, initOverrides?: RequestInit | runtime.InitOverrideFunction): Promise<runtime.ApiResponse<SubscriptionsImported>> {
        if (requestParameters['file'] == null) {
            throw new runtime.RequiredError(
                'file',
                'Required parameter "file" was null or undefined when calling import().'
            );
        }

        if (requestParameters['organizationId'] == null) {
            throw new runtime.RequiredError(
                'organizationId',
                'Required parameter "organizationId" was null or undefined when calling import().'
            );
        }

        const queryParameters: any = {};

        const headerParameters: runtime.HTTPHeaders = {};

        if (this.configuration && this.configuration.accessToken) {
            const token = this.configuration.accessToken;
            const tokenString = await token("HTTPBearer", []);

            if (tokenString) {
                headerParameters["Authorization"] = `Bearer ${tokenString}`;
            }
        }
        const consumes: runtime.Consume[] = [
            { contentType: 'multipart/form-data' },
        ];
        // @ts-ignore: canConsumeForm may be unused
        const canConsumeForm = runtime.canConsumeForm(consumes);

        let formParams: { append(param: string, value: any): any };
        let useForm = false;
        // use FormData to transmit files using content-type "multipart/form-data"
        useForm = canConsumeForm;
        if (useForm) {
            formParams = new FormData();
        } else {
            formParams = new URLSearchParams();
        }

        if (requestParameters['file'] != null) {
            formParams.append('file', requestParameters['file'] as any);
        }

        if (requestParameters['organizationId'] != null) {
            formParams.append('organization_id', requestParameters['organizationId'] as any);
        }

        const response = await this.request({
            path: `/v1/subscriptions/import`,
            method: 'POST',
            headers: headerParameters,
            query: queryParameters,
            body: formParams,
        }, initOverrides);

        return new runtime.JSONApiResponse(response);
    }

    /**
     * Import subscriptions from a CSV file.
     * Import Subscriptions
     */
    async import(requestParameters: SubscriptionsApiImportRequest, initOverrides?: RequestInit | runtime.InitOverrideFunction): Promise<SubscriptionsImported> {
        const response = await this.importRaw(requestParameters, initOverrides);
        return await response.value();
    }

    /**
     * List subscriptions.
     * List Subscriptions
     */
    async listRaw(requestParameters: SubscriptionsApiListRequest, initOverrides?: RequestInit | runtime.InitOverrideFunction): Promise<runtime.ApiResponse<ListResourceSubscription>> {
        const queryParameters: any = {};

        if (requestParameters['organizationId'] != null) {
            queryParameters['organization_id'] = requestParameters['organizationId'];
        }

        if (requestParameters['productId'] != null) {
            queryParameters['product_id'] = requestParameters['productId'];
        }

        if (requestParameters['type'] != null) {
            queryParameters['type'] = requestParameters['type'];
        }

        if (requestParameters['active'] != null) {
            queryParameters['active'] = requestParameters['active'];
        }

        if (requestParameters['page'] != null) {
            queryParameters['page'] = requestParameters['page'];
        }

        if (requestParameters['limit'] != null) {
            queryParameters['limit'] = requestParameters['limit'];
        }

        if (requestParameters['sorting'] != null) {
            queryParameters['sorting'] = requestParameters['sorting'];
        }

        const headerParameters: runtime.HTTPHeaders = {};

        if (this.configuration && this.configuration.accessToken) {
            const token = this.configuration.accessToken;
            const tokenString = await token("HTTPBearer", []);

            if (tokenString) {
                headerParameters["Authorization"] = `Bearer ${tokenString}`;
            }
        }
        const response = await this.request({
            path: `/v1/subscriptions/`,
            method: 'GET',
            headers: headerParameters,
            query: queryParameters,
        }, initOverrides);

        return new runtime.JSONApiResponse(response);
    }

    /**
     * List subscriptions.
     * List Subscriptions
     */
    async list(requestParameters: SubscriptionsApiListRequest = {}, initOverrides?: RequestInit | runtime.InitOverrideFunction): Promise<ListResourceSubscription> {
        const response = await this.listRaw(requestParameters, initOverrides);
        return await response.value();
    }

}
